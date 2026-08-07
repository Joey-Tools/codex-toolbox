from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import selectors
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_scheduler_doctor_tests",
    SCRIPT_PATH,
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PUBLIC_SHA = "1" * 40
PRIVATE_SHA = "2" * 40


_SCHEDULER_DOCTOR_TEST_NAMESPACE: Path | None = None
_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV = "CODEX_SCHEDULER_DOCTOR_TEST_ANCHOR"
_SCHEDULER_DOCTOR_TEST_EXPECTED_ANCHOR_ENV = (
    "CODEX_SCHEDULER_DOCTOR_TEST_EXPECTED_ANCHOR"
)
_SCHEDULER_DOCTOR_TEST_EXPECTED_LINUX_STICKY_ROOT_ENV = (
    "CODEX_SCHEDULER_DOCTOR_TEST_EXPECTED_LINUX_STICKY_ROOT"
)
_SCHEDULER_DOCTOR_TEST_CONTAINER_NAME = ".codex-tmp"
_SCHEDULER_DOCTOR_TEST_NAMESPACE_NAME = "scheduler-doctor"
_SCHEDULER_DOCTOR_TEST_LOCK_NAME = ".session.lock"
_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME = ".liveness.lock"
_SCHEDULER_DOCTOR_TEST_LIVENESS_REGISTRY_ENV = (
    "CODEX_SCHEDULER_DOCTOR_TEST_LIVENESS_FDS"
)
_SCHEDULER_DOCTOR_TEST_SESSION_PREFIX = "session."
_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX = ".session-staging."
_SCHEDULER_DOCTOR_TEST_DELETE_PREFIX = ".delete.scheduler-session."
_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME = "payload"
_SCHEDULER_DOCTOR_TEST_DELETE_NONCE_BYTES = 16
_SCHEDULER_DOCTOR_TEST_DELETE_CREATE_ATTEMPTS = 32
_SCHEDULER_DOCTOR_TEST_NAMESPACE_ENTRY_LIMIT = 1024
_SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_ENTRY_LIMIT = 10_000
_SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_DEPTH_LIMIT = 64
_SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_TIMEOUT_SECONDS = 30.0
_SCHEDULER_DOCTOR_TEST_LEASE_TIMEOUT_SECONDS = 60.0
_SCHEDULER_DOCTOR_TEST_LEASE_RETRY_SECONDS = 0.05
_SCHEDULER_DOCTOR_TEST_GUARDIAN_EOF_TIMEOUT_SECONDS = 5.0
_SCHEDULER_DOCTOR_TEST_DARWIN_TEMP_SCAN_ENTRY_LIMIT = 4096
_SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT = Path("/tmp")
_SCHEDULER_DOCTOR_TEST_LINUX_STICKY_FALLBACK_PREFIX = (
    ".codex-scheduler-doctor-"
)
_SCHEDULER_DOCTOR_TEST_SESSION: _SchedulerDoctorActiveSessionBinding | None = None
_SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD: int | None = None
_SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE: (
    _SchedulerDoctorSessionCleanupFailure | None
) = None
_SCHEDULER_DOCTOR_TEST_HOST_PLATFORM = sys.platform
_SCHEDULER_DOCTOR_TEST_ORIGINAL_POPEN = subprocess.Popen
_SCHEDULER_DOCTOR_TEST_POPEN_INSTALLED = False


def _wait_for_scheduler_guardian_fifo_eof(
    read_fd: int,
    *,
    deadline: float,
) -> None:
    # kqueue does not consistently surface a post-read FIFO writer close as a
    # fresh event on macOS; select(2) does, while retaining an event-driven
    # absolute-deadline wait.
    with selectors.SelectSelector() as selector:
        selector.register(read_fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "a launched process still retains the FIFO liveness writer"
                )
            events = selector.select(remaining)
            if not events:
                continue
            try:
                terminal = os.read(read_fd, 1)
            except BlockingIOError:
                continue
            if terminal:
                raise AssertionError(
                    "the FIFO liveness channel contained unexpected payload bytes"
                )
            return


class _SchedulerDoctorTestCandidateUnavailable(RuntimeError):
    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


class _SchedulerDoctorDeleteQuarantineFailure(RuntimeError):
    def __init__(self, retained_path: Path | None, reason: str) -> None:
        super().__init__(reason)
        self.retained_path = retained_path


class _SchedulerDoctorQuarantineTransitionFailure(RuntimeError):
    def __init__(self, retained_name: str, reason: str) -> None:
        super().__init__(reason)
        self.retained_name = retained_name


@dataclass(frozen=True)
class _SchedulerDoctorStaleEntryPlan:
    name: str
    identity: tuple[int, int, int]
    children: tuple["_SchedulerDoctorStaleEntryPlan", ...] | None
    owner_private_directory: bool = False


@dataclass
class _SchedulerDoctorStaleCleanupBudget:
    deadline: float
    remaining_entries: int
    depth_limit: int


@dataclass(frozen=True)
class _SchedulerDoctorActiveSessionBinding:
    path: Path
    namespace_path: Path
    namespace_descriptor: int
    namespace_identity: tuple[int, int, int]
    namespace_mount_identity: tuple[int, int | None]
    descriptor: int
    identity: tuple[int, int, int]
    mount_identity: tuple[int, int | None]
    liveness_descriptor: int
    liveness_identity: tuple[int, int, int]


@dataclass(frozen=True)
class _SchedulerDoctorStaleSessionCandidate:
    name: str
    identity: tuple[int, int, int]
    liveness_identity: tuple[int, int, int] | None
    busy: bool
    plans: tuple[_SchedulerDoctorStaleEntryPlan, ...] | None
    staging: bool


@dataclass(frozen=True)
class _SchedulerDoctorDeleteQuarantineCandidate:
    name: str
    identity: tuple[int, int, int]
    payload_identity: tuple[int, int, int] | None
    liveness_identity: tuple[int, int, int] | None
    liveness_present: bool
    busy: bool
    plans: tuple[_SchedulerDoctorStaleEntryPlan, ...] | None


@dataclass(frozen=True)
class _SchedulerDoctorDeleteQuarantineBinding:
    name: str
    identity: tuple[int, int, int]
    descriptor: int
    payload_identity: tuple[int, int, int] | None


@dataclass(frozen=True)
class _SchedulerDoctorAbandonedDescriptorCustody:
    role: str
    descriptor: int
    identity: tuple[int, int, int] | None
    state: str


@dataclass(frozen=True)
class _SchedulerDoctorSessionCleanupFailure:
    retained_path: Path | None
    reason: str
    abandoned_custody: tuple[
        _SchedulerDoctorAbandonedDescriptorCustody, ...
    ] = ()


@dataclass(frozen=True)
class _SchedulerDoctorBoundNamespaceCandidate:
    path: Path
    identity: tuple[int, int, int]
    access_policy: tuple[int, int, int]
    sticky_root_path: Path | None = None
    sticky_root_identity: tuple[int, int, int] | None = None
    sticky_root_access_policy: tuple[int, int, int] | None = None
    effective_uid: int | None = None

    def __fspath__(self) -> str:
        return os.fspath(self.path)


@dataclass(frozen=True)
class _SchedulerDoctorLinuxStickyFallbackCandidate:
    pass


_SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE = (
    _SchedulerDoctorLinuxStickyFallbackCandidate()
)


def _scheduler_doctor_linux_sticky_fallback_path(
    *,
    effective_uid: int | None = None,
) -> Path:
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    return _SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT / (
        _SCHEDULER_DOCTOR_TEST_LINUX_STICKY_FALLBACK_PREFIX
        + str(selected_uid)
    )


def _scheduler_doctor_candidate_path(
    candidate: (
        Path
        | _SchedulerDoctorBoundNamespaceCandidate
        | _SchedulerDoctorLinuxStickyFallbackCandidate
    ),
) -> Path:
    if isinstance(candidate, _SchedulerDoctorBoundNamespaceCandidate):
        return candidate.path
    if isinstance(candidate, _SchedulerDoctorLinuxStickyFallbackCandidate):
        return _scheduler_doctor_linux_sticky_fallback_path()
    return candidate


def _scheduler_doctor_linux_sticky_component_policy_is_safe(
    path: Path,
    root: Path,
    access_policy: tuple[int, int, int],
    effective_uid: int,
) -> bool:
    mode, uid, _gid = access_policy
    if path == Path("/tmp"):
        return uid == 0 and mode == 0o1777
    if path == root:
        return uid == effective_uid and mode == 0o1777
    return uid in {0, effective_uid} and not mode & 0o022


def _scheduler_doctor_linux_sticky_root_binding(
    root: Path,
    *,
    effective_uid: int | None = None,
) -> tuple[int, tuple[int, int, int], tuple[int, int, int]] | None:
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    root = Path(root)
    if not root.is_absolute() or root == Path("/"):
        return None
    root = Path(os.path.abspath(root))
    components = root.parts[1:]
    if not components or len(components) > MODULE.MIRROR_PRIVATE_CONTROL_MAX_ANCESTORS:
        return None

    current_path = Path("/")
    current_fd = os.open(current_path, MODULE._source_directory_flags())
    try:
        for component in components:
            component_path = current_path / component
            try:
                os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            except OSError as error:
                raise RuntimeError(
                    "cannot inspect Linux scheduler-doctor sticky root "
                    f"component: {component_path}: {error}"
                ) from error
            try:
                child_fd, _child_identity, child_access_policy = (
                    MODULE._bind_mirror_audit_child_directory(
                        current_fd,
                        current_path,
                        component,
                        "Linux scheduler-doctor sticky-root component",
                    )
                )
            except MODULE.SyncError as error:
                raise RuntimeError(
                    "cannot bind Linux scheduler-doctor sticky root "
                    f"component: {component_path}: {error}"
                ) from error
            if not _scheduler_doctor_linux_sticky_component_policy_is_safe(
                component_path,
                root,
                child_access_policy,
                selected_uid,
            ):
                close_failures = _close_scheduler_doctor_candidate_descriptors(
                    (child_fd,)
                )
                message = (
                    "Linux scheduler-doctor sticky root has an unsafe access "
                    f"policy: {component_path}"
                )
                if close_failures:
                    message += "; " + "; ".join(close_failures)
                raise RuntimeError(message)
            previous_fd = current_fd
            current_fd = -1
            close_failures = _close_scheduler_doctor_candidate_descriptors(
                (previous_fd,)
            )
            if close_failures:
                close_failures.extend(
                    _close_scheduler_doctor_candidate_descriptors((child_fd,))
                )
                raise RuntimeError(
                    "cannot advance Linux scheduler-doctor sticky-root "
                    f"binding: {component_path}: {'; '.join(close_failures)}"
                )
            current_fd = child_fd
            current_path = component_path
        metadata = os.fstat(current_fd)
        result = (
            current_fd,
            MODULE._mirror_object_identity(metadata),
            MODULE._mirror_access_policy(metadata),
        )
        current_fd = -1
        return result
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (current_fd,)
        )
        if close_failures:
            primary = sys.exc_info()[1]
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(
                "cannot close Linux scheduler-doctor sticky-root descriptor: "
                + message
            )


def _scheduler_doctor_linux_sticky_fallback_binding(
) -> _SchedulerDoctorBoundNamespaceCandidate | None:
    effective_uid = os.geteuid()
    sticky_root = _SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT
    sticky_binding = _scheduler_doctor_linux_sticky_root_binding(
        sticky_root,
        effective_uid=effective_uid,
    )
    if sticky_binding is None:
        return None
    sticky_fd, sticky_identity, sticky_access_policy = sticky_binding
    fallback = _scheduler_doctor_linux_sticky_fallback_path(
        effective_uid=effective_uid
    )
    fallback_fd = -1
    try:
        try:
            os.mkdir(fallback.name, 0o700, dir_fd=sticky_fd)
        except FileExistsError:
            pass
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                try:
                    os.stat(
                        fallback.name,
                        dir_fd=sticky_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return None
                except OSError as inspect_error:
                    raise RuntimeError(
                        "cannot prove the Linux scheduler-doctor sticky "
                        f"fallback is absent: {fallback}: {inspect_error}"
                    ) from inspect_error
                raise RuntimeError(
                    "Linux scheduler-doctor sticky fallback appeared after "
                    f"allocation failed: {fallback}"
                ) from error
            raise RuntimeError(
                "cannot create Linux scheduler-doctor sticky fallback: "
                f"{fallback}: {error}"
            ) from error
        try:
            fallback_fd, fallback_identity, fallback_access_policy = (
                MODULE._bind_mirror_audit_child_directory(
                    sticky_fd,
                    sticky_root,
                    fallback.name,
                    "Linux scheduler-doctor sticky fallback",
                )
            )
        except MODULE.SyncError as error:
            raise RuntimeError(
                "Linux scheduler-doctor sticky fallback has an unsafe type, "
                f"owner, or mode: {fallback}"
            ) from error
        mode, uid, _gid = fallback_access_policy
        if uid != effective_uid or mode != 0o700:
            raise RuntimeError(
                "Linux scheduler-doctor sticky fallback has an unsafe type, "
                f"owner, or mode: {fallback}"
            )
        return _SchedulerDoctorBoundNamespaceCandidate(
            fallback,
            fallback_identity,
            fallback_access_policy,
            sticky_root,
            sticky_identity,
            sticky_access_policy,
            effective_uid,
        )
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (fallback_fd, sticky_fd)
        )
        if close_failures:
            primary = sys.exc_info()[1]
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(
                "cannot close Linux scheduler-doctor sticky fallback "
                f"descriptors: {message}"
            )


def _scheduler_doctor_bound_candidate_uses_sticky_root(
    candidate: _SchedulerDoctorBoundNamespaceCandidate,
) -> bool:
    values = (
        candidate.sticky_root_path,
        candidate.sticky_root_identity,
        candidate.sticky_root_access_policy,
        candidate.effective_uid,
    )
    if all(value is None for value in values):
        return False
    if any(value is None for value in values):
        raise RuntimeError(
            "Linux scheduler-doctor sticky fallback receipt is incomplete"
        )
    assert candidate.sticky_root_path is not None
    assert candidate.effective_uid is not None
    if candidate.path != _scheduler_doctor_linux_sticky_fallback_path(
        effective_uid=candidate.effective_uid
    ):
        raise RuntimeError(
            "Linux scheduler-doctor sticky fallback receipt path is invalid"
        )
    return True


def _scheduler_doctor_rebind_sticky_candidate(
    candidate: _SchedulerDoctorBoundNamespaceCandidate,
) -> tuple[int, tuple[int, int, int], tuple[int, int, int]]:
    if not _scheduler_doctor_bound_candidate_uses_sticky_root(candidate):
        raise RuntimeError(
            "Linux scheduler-doctor sticky fallback receipt is missing"
        )
    assert candidate.sticky_root_path is not None
    assert candidate.sticky_root_identity is not None
    assert candidate.sticky_root_access_policy is not None
    assert candidate.effective_uid is not None
    if os.geteuid() != candidate.effective_uid:
        raise RuntimeError(
            "Linux scheduler-doctor effective uid changed after binding"
        )
    sticky_binding = _scheduler_doctor_linux_sticky_root_binding(
        candidate.sticky_root_path,
        effective_uid=candidate.effective_uid,
    )
    if sticky_binding is None:
        raise RuntimeError(
            "Linux scheduler-doctor sticky root changed after binding"
        )
    sticky_fd, sticky_identity, sticky_access_policy = sticky_binding
    fallback_fd = -1
    returned_fd = -1
    try:
        if (
            sticky_identity != candidate.sticky_root_identity
            or sticky_access_policy != candidate.sticky_root_access_policy
        ):
            raise RuntimeError(
                "Linux scheduler-doctor sticky root changed after binding"
            )
        try:
            fallback_fd, fallback_identity, fallback_access_policy = (
                MODULE._bind_mirror_audit_child_directory(
                    sticky_fd,
                    candidate.sticky_root_path,
                    candidate.path.name,
                    "Linux scheduler-doctor sticky fallback",
                )
            )
        except MODULE.SyncError as error:
            raise RuntimeError(
                "Linux scheduler-doctor sticky fallback changed after binding"
            ) from error
        mode, uid, _gid = fallback_access_policy
        if (
            uid != candidate.effective_uid
            or mode != 0o700
            or fallback_identity != candidate.identity
            or fallback_access_policy != candidate.access_policy
        ):
            raise RuntimeError(
                "Linux scheduler-doctor sticky fallback changed after binding"
            )
        returned_fd = fallback_fd
        fallback_fd = -1
        return returned_fd, candidate.identity, candidate.access_policy
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (fallback_fd, sticky_fd)
        )
        if close_failures and returned_fd >= 0:
            close_failures.extend(
                _close_scheduler_doctor_candidate_descriptors((returned_fd,))
            )
        if close_failures:
            primary = sys.exc_info()[1]
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(
                "cannot close Linux scheduler-doctor sticky fallback "
                f"descriptors: {message}"
            )


def _bind_scheduler_doctor_test_root(
    path: Path,
) -> tuple[int, tuple[int, int, int], tuple[int, int, int]]:
    effective_uid = os.geteuid()
    path = Path(os.path.abspath(path))
    fallback = _scheduler_doctor_linux_sticky_fallback_path(
        effective_uid=effective_uid
    )
    if not sys.platform.startswith("linux") or not (
        path == fallback or path.is_relative_to(fallback)
    ):
        return MODULE._bind_mirror_trusted_account_home(path)

    sticky_root = _SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT
    sticky_binding = _scheduler_doctor_linux_sticky_root_binding(
        sticky_root,
        effective_uid=effective_uid,
    )
    if sticky_binding is None:
        raise RuntimeError("Linux scheduler-doctor sticky root is unavailable")
    current_fd, _sticky_identity, _sticky_access_policy = sticky_binding
    relative_parts = (fallback.name,) + path.relative_to(fallback).parts
    current_path = sticky_root
    try:
        for index, component in enumerate(relative_parts):
            component_path = current_path / component
            try:
                child_fd, _child_identity, child_access_policy = (
                    MODULE._bind_mirror_audit_child_directory(
                        current_fd,
                        current_path,
                        component,
                        "Linux scheduler-doctor sticky fallback component",
                    )
                )
            except MODULE.SyncError as error:
                if index == 0:
                    raise RuntimeError(
                        "cannot bind Linux scheduler-doctor sticky fallback "
                        f"root: {component_path}: {error}"
                    ) from error
                raise
            mode, uid, _gid = child_access_policy
            if (
                uid != effective_uid
                or mode & 0o022
                or (index == 0 and mode != 0o700)
            ):
                close_failures = _close_scheduler_doctor_candidate_descriptors(
                    (child_fd,)
                )
                if index == 0:
                    message = (
                        "Linux scheduler-doctor sticky fallback root has an "
                        f"unsafe access policy: {component_path}"
                    )
                else:
                    message = (
                        "canonical account-home ancestors must be "
                        "root/current-owned and not group/world writable: "
                        f"{component_path}"
                    )
                if close_failures:
                    raise RuntimeError(
                        message + "; " + "; ".join(close_failures)
                    )
                if index == 0:
                    raise RuntimeError(message)
                raise MODULE.SyncError(message)
            previous_fd = current_fd
            current_fd = -1
            close_failures = _close_scheduler_doctor_candidate_descriptors(
                (previous_fd,)
            )
            if close_failures:
                close_failures.extend(
                    _close_scheduler_doctor_candidate_descriptors((child_fd,))
                )
                raise RuntimeError(
                    "cannot advance Linux scheduler-doctor sticky fallback "
                    f"binding: {component_path}: {'; '.join(close_failures)}"
                )
            current_fd = child_fd
            current_path = component_path
        metadata = os.fstat(current_fd)
        result = (
            current_fd,
            MODULE._mirror_object_identity(metadata),
            MODULE._mirror_access_policy(metadata),
        )
        current_fd = -1
        return result
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (current_fd,)
        )
        if close_failures:
            primary = sys.exc_info()[1]
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(
                "cannot close Linux scheduler-doctor sticky fallback "
                f"component descriptor: {message}"
            )


def _bind_scheduler_doctor_fixture_account_home(
    path: Path,
    *,
    fixture_root: Path,
    production_binder: Callable[
        [Path],
        tuple[int, tuple[int, int, int], tuple[int, int, int]],
    ],
) -> tuple[int, tuple[int, int, int], tuple[int, int, int]]:
    # Keep the production account-home policy unchanged. Only the per-test
    # root selected beneath the receipt-bound Linux sticky fallback and its
    # synthetic descendants use the fixture's equivalent descriptor-safe
    # ancestry binder.
    path = Path(os.path.abspath(path))
    fixture_root = Path(os.path.abspath(fixture_root))
    fallback = _scheduler_doctor_linux_sticky_fallback_path()
    if (
        not (path == fixture_root or path.is_relative_to(fixture_root))
        or not sys.platform.startswith("linux")
        or fixture_root == fallback
        or not fixture_root.is_relative_to(fallback)
    ):
        return production_binder(path)
    return _bind_scheduler_doctor_test_root(path)


def _scheduler_doctor_private_platform_parent_binding(
    candidate: Path,
    *,
    label: str,
) -> _SchedulerDoctorBoundNamespaceCandidate | None:
    if not candidate.is_absolute() or candidate == Path("/"):
        return None
    components = candidate.parts[1:]
    if not components or len(components) > MODULE.MIRROR_PRIVATE_CONTROL_MAX_ANCESTORS:
        return None

    current = Path("/")
    for index, component in enumerate(components):
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RuntimeError(
                f"cannot inspect {label} component: {current}: {error}"
            ) from error
        mode, uid, _gid = MODULE._mirror_access_policy(metadata)
        is_terminal = index == len(components) - 1
        owner_is_acceptable = (
            uid == os.geteuid()
            if is_terminal
            else uid in {0, os.geteuid()}
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or not owner_is_acceptable
            or mode & 0o022
        ):
            return None

    descriptor = -1
    try:
        descriptor, identity, access_policy = (
            MODULE._bind_mirror_trusted_account_home(candidate)
        )
        return _SchedulerDoctorBoundNamespaceCandidate(
            candidate,
            identity,
            access_policy,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _scheduler_doctor_linux_runtime_parent_binding(
    candidate: Path,
) -> _SchedulerDoctorBoundNamespaceCandidate | None:
    return _scheduler_doctor_private_platform_parent_binding(
        candidate,
        label="Linux scheduler-doctor runtime parent",
    )


def _scheduler_doctor_darwin_temp_parent(
    candidate: Path,
) -> _SchedulerDoctorBoundNamespaceCandidate | None:
    return _scheduler_doctor_private_platform_parent_binding(
        candidate,
        label="Darwin scheduler-doctor temp parent",
    )


def _scheduler_doctor_test_platform_anchor_parents(
) -> tuple[
    Path
    | _SchedulerDoctorBoundNamespaceCandidate
    | _SchedulerDoctorLinuxStickyFallbackCandidate,
    ...,
]:
    candidates: list[
        Path
        | _SchedulerDoctorBoundNamespaceCandidate
        | _SchedulerDoctorLinuxStickyFallbackCandidate
    ] = []
    if sys.platform == "darwin":
        darwin_temp_root = Path("/private/var/folders")
        configured_temp = os.environ.get("TMPDIR")
        configured_parent: _SchedulerDoctorBoundNamespaceCandidate | None = None
        if configured_temp:
            candidate = Path(configured_temp)
            if candidate.is_absolute():
                resolved = Path(os.path.realpath(candidate))
                if resolved.is_relative_to(darwin_temp_root):
                    configured_parent = (
                        _scheduler_doctor_darwin_temp_parent(resolved)
                    )
                    if configured_parent is not None:
                        candidates.append(configured_parent)
        getconf_environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
        }
        if configured_parent is not None:
            getconf_environment["TMPDIR"] = str(configured_parent.path)
        try:
            result = subprocess.run(
                ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
                env=getconf_environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        else:
            output_lines = result.stdout.splitlines()
            if result.returncode == 0 and len(output_lines) == 1:
                candidate = Path(output_lines[0])
                if candidate.is_absolute():
                    resolved = Path(os.path.realpath(candidate))
                    if resolved.is_relative_to(darwin_temp_root):
                        bound_parent = _scheduler_doctor_darwin_temp_parent(
                            resolved
                        )
                        if bound_parent is not None:
                            candidates.append(bound_parent)
        if not candidates:
            for scanned_parent in _bounded_darwin_user_temp_directories():
                bound_parent = _scheduler_doctor_darwin_temp_parent(
                    scanned_parent
                )
                if bound_parent is not None:
                    candidates.append(bound_parent)
    if sys.platform.startswith("linux"):
        runtime_root = Path("/run/user") / str(os.geteuid())
        runtime_candidates: list[Path] = []
        runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_directory:
            candidate = Path(runtime_directory)
            if candidate.is_absolute():
                normalized = Path(os.path.abspath(candidate))
                if candidate == normalized and (
                    candidate == runtime_root
                    or candidate.is_relative_to(runtime_root)
                ):
                    runtime_candidates.append(candidate)
        runtime_candidates.append(runtime_root)
        for candidate in dict.fromkeys(runtime_candidates):
            binding = _scheduler_doctor_linux_runtime_parent_binding(candidate)
            if binding is not None:
                candidates.append(binding)
        candidates.append(_SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE)
    return tuple(dict.fromkeys(candidates))


def _validate_owner_private_directory(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"test fixture path is not a real directory: {path}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"test fixture path is not owned by the current uid: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError(f"test fixture path is not mode 0700: {path}")
    return metadata


def _scheduler_doctor_metadata_is_owner_private_directory(
    metadata: os.stat_result,
    *,
    effective_uid: int | None = None,
) -> bool:
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == selected_uid
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _scheduler_doctor_platform_parent_in_scope(path: Path) -> bool:
    if sys.platform == "darwin":
        root = Path("/private/var/folders")
        return path != root and path.is_relative_to(root)
    if sys.platform.startswith("linux"):
        root = Path("/run/user") / str(os.geteuid())
        return path == root or path.is_relative_to(root)
    return False


def _open_scanned_scheduler_doctor_directory(
    parent_fd: int,
    name: str,
) -> tuple[int, os.stat_result] | None:
    try:
        named_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(named_metadata.st_mode):
        return None
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            MODULE._source_directory_flags(),
            dir_fd=parent_fd,
        )
        descriptor_metadata = os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        return None
    if (
        MODULE._mirror_object_identity(named_metadata)
        != MODULE._mirror_object_identity(descriptor_metadata)
        or MODULE._mirror_access_policy(named_metadata)
        != MODULE._mirror_access_policy(descriptor_metadata)
    ):
        os.close(descriptor)
        raise RuntimeError(
            "Darwin user temp directory changed while binding its component"
        )
    return descriptor, descriptor_metadata


def _bounded_darwin_user_temp_directories() -> tuple[Path, ...]:
    root = Path("/private/var/folders")
    try:
        root_fd = os.open(Path("/"), MODULE._source_directory_flags())
    except OSError:
        return ()
    try:
        for component in root.parts[1:]:
            opened_component = _open_scanned_scheduler_doctor_directory(
                root_fd,
                component,
            )
            if opened_component is None:
                os.close(root_fd)
                return ()
            component_fd, component_metadata = opened_component
            component_mode, component_uid, _component_gid = (
                MODULE._mirror_access_policy(component_metadata)
            )
            if component_uid not in {0, os.geteuid()} or component_mode & 0o022:
                os.close(component_fd)
                os.close(root_fd)
                return ()
            os.close(root_fd)
            root_fd = component_fd
    except BaseException:
        os.close(root_fd)
        raise
    candidates: list[Path] = []
    entry_count = 0

    def consume_entry() -> None:
        nonlocal entry_count
        entry_count += 1
        if entry_count > _SCHEDULER_DOCTOR_TEST_DARWIN_TEMP_SCAN_ENTRY_LIMIT:
            raise RuntimeError(
                "Darwin user temp directory scan exceeded its entry limit"
            )

    try:
        with os.scandir(root_fd) as bucket_entries:
            for bucket_entry in bucket_entries:
                consume_entry()
                opened_bucket = _open_scanned_scheduler_doctor_directory(
                    root_fd,
                    bucket_entry.name,
                )
                if opened_bucket is None:
                    continue
                bucket_fd, bucket_metadata = opened_bucket
                try:
                    bucket_mode, bucket_uid, _bucket_gid = (
                        MODULE._mirror_access_policy(bucket_metadata)
                    )
                    if bucket_uid not in {0, os.geteuid()} or bucket_mode & 0o022:
                        continue
                    with os.scandir(bucket_fd) as account_entries:
                        for account_entry in account_entries:
                            consume_entry()
                            opened_account = _open_scanned_scheduler_doctor_directory(
                                bucket_fd,
                                account_entry.name,
                            )
                            if opened_account is None:
                                continue
                            account_fd, account_metadata = opened_account
                            try:
                                account_mode, account_uid, _account_gid = (
                                    MODULE._mirror_access_policy(account_metadata)
                                )
                                if (
                                    account_uid != os.geteuid()
                                    or account_mode & 0o022
                                ):
                                    continue
                                opened_temp = (
                                    _open_scanned_scheduler_doctor_directory(
                                        account_fd,
                                        "T",
                                    )
                                )
                                if opened_temp is None:
                                    continue
                                temp_fd, temp_metadata = opened_temp
                                try:
                                    if not (
                                        _scheduler_doctor_metadata_is_owner_private_directory(
                                            temp_metadata
                                        )
                                    ):
                                        continue
                                    candidates.append(
                                        root
                                        / bucket_entry.name
                                        / account_entry.name
                                        / "T"
                                    )
                                finally:
                                    os.close(temp_fd)
                            finally:
                                os.close(account_fd)
                finally:
                    os.close(bucket_fd)
    finally:
        os.close(root_fd)
    return tuple(sorted(candidates, key=os.fsencode))


def _scheduler_doctor_test_namespace_candidates(
) -> tuple[
    Path
    | _SchedulerDoctorBoundNamespaceCandidate
    | _SchedulerDoctorLinuxStickyFallbackCandidate,
    ...,
]:
    candidates: list[
        Path
        | _SchedulerDoctorBoundNamespaceCandidate
        | _SchedulerDoctorLinuxStickyFallbackCandidate
    ] = []
    linux_sticky_fallback_available = False
    configured_anchor = os.environ.get(_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV)
    if configured_anchor:
        override = Path(configured_anchor)
        if not override.is_absolute():
            raise RuntimeError(
                f"{_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV} must be absolute"
            )
        candidates.append(override)
    for candidate_entry in _scheduler_doctor_test_platform_anchor_parents():
        if isinstance(
            candidate_entry,
            _SchedulerDoctorLinuxStickyFallbackCandidate,
        ):
            linux_sticky_fallback_available = True
            continue
        candidate = (
            candidate_entry.path
            if isinstance(
                candidate_entry,
                _SchedulerDoctorBoundNamespaceCandidate,
            )
            else candidate_entry
        )
        resolved = Path(os.path.realpath(candidate))
        if (
            isinstance(candidate_entry, _SchedulerDoctorBoundNamespaceCandidate)
            and resolved != candidate
        ):
            raise RuntimeError(
                "scheduler-doctor platform parent changed after binding"
            )
        if resolved == candidate and _scheduler_doctor_platform_parent_in_scope(
            resolved
        ):
            candidates.append(candidate_entry)
    candidates.append(REPO_ROOT)
    if linux_sticky_fallback_available:
        candidates.append(_SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE)

    unique: list[
        Path
        | _SchedulerDoctorBoundNamespaceCandidate
        | _SchedulerDoctorLinuxStickyFallbackCandidate
    ] = []
    seen: set[Path] = set()
    sticky_fallback_seen = False
    for candidate_entry in candidates:
        if isinstance(
            candidate_entry,
            _SchedulerDoctorLinuxStickyFallbackCandidate,
        ):
            if not sticky_fallback_seen:
                unique.append(candidate_entry)
                sticky_fallback_seen = True
            continue
        candidate = (
            candidate_entry.path
            if isinstance(
                candidate_entry,
                _SchedulerDoctorBoundNamespaceCandidate,
            )
            else candidate_entry
        )
        if not candidate.is_absolute():
            continue
        resolved = Path(os.path.realpath(candidate))
        if (
            isinstance(candidate_entry, _SchedulerDoctorBoundNamespaceCandidate)
            and resolved != candidate
        ):
            raise RuntimeError(
                "scheduler-doctor platform parent changed after binding"
            )
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(
            candidate_entry
            if isinstance(
                candidate_entry,
                _SchedulerDoctorBoundNamespaceCandidate,
            )
            else resolved
        )
    return tuple(unique)


def _validate_trusted_scheduler_doctor_test_root(path: Path) -> None:
    descriptor, _identity, _access_policy = _bind_scheduler_doctor_test_root(
        path
    )
    os.close(descriptor)


def _scheduler_doctor_test_object_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _scheduler_doctor_test_anchor_is_stably_unsuitable(
    error: MODULE.SyncError,
    candidate: Path,
) -> bool:
    messages = {
        "canonical account home must be owned by the current uid and not "
        "group/world writable",
        "canonical account home exceeds its ancestor limit",
    }
    current = Path("/")
    for component in candidate.parts[1:]:
        current /= component
        messages.add(
            "canonical account-home ancestors must be root/current-owned and "
            f"not group/world writable: {current}"
        )
    return str(error) in messages


def _cleanup_scheduler_doctor_candidate_allocation(
    created: list[tuple[Path, tuple[int, int, int] | None]],
) -> None:
    for path, expected_identity in reversed(created):
        if expected_identity is None:
            raise RuntimeError(
                f"scheduler-doctor fixture allocation identity is unavailable; "
                f"retained for inspection: {path}"
            )
        metadata = path.lstat()
        if _scheduler_doctor_test_object_identity(metadata) != expected_identity:
            raise RuntimeError(
                f"scheduler-doctor fixture allocation changed before cleanup: {path}"
            )
        _validate_owner_private_directory(path)
        path.rmdir()
        if path.exists() or path.is_symlink():
            raise RuntimeError(
                f"scheduler-doctor fixture allocation cleanup failed: {path}"
            )


def _close_scheduler_doctor_candidate_descriptors(
    descriptors: tuple[int, ...],
) -> list[str]:
    failures: list[str] = []
    for descriptor in descriptors:
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            failures.append(f"descriptor close failed: {error}")
    return failures


def _close_and_cleanup_scheduler_doctor_candidate(
    primary: BaseException,
    descriptors: tuple[int, ...],
    created: list[tuple[Path, tuple[int, int, int] | None]],
) -> None:
    secondary = _close_scheduler_doctor_candidate_descriptors(descriptors)
    try:
        _cleanup_scheduler_doctor_candidate_allocation(created)
    except BaseException as error:
        secondary.append(f"allocation cleanup failed: {error}")
    if secondary:
        raise RuntimeError(f"{primary}; {'; '.join(secondary)}") from primary


def _probe_scheduler_doctor_test_namespace_lock(
    namespace: Path,
    namespace_fd: int,
) -> None:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    expected_identity: tuple[int, int, int] | None = None
    try:
        try:
            metadata = os.stat(
                _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
                dir_fd=namespace_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=namespace_fd,
                )
            except OSError as error:
                if error.errno == errno.EEXIST:
                    try:
                        metadata = os.stat(
                            _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
                            dir_fd=namespace_fd,
                            follow_symlinks=False,
                        )
                    except OSError as existing_error:
                        if existing_error.errno in {
                            errno.EACCES,
                            errno.EPERM,
                            errno.EROFS,
                        }:
                            raise _SchedulerDoctorTestCandidateUnavailable(
                                existing_error
                            ) from existing_error
                        raise
                    expected_identity = _scheduler_doctor_test_object_identity(
                        metadata
                    )
                    try:
                        descriptor = os.open(
                            _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
                            flags,
                            dir_fd=namespace_fd,
                        )
                    except OSError as existing_error:
                        if existing_error.errno in {
                            errno.EACCES,
                            errno.EPERM,
                            errno.EROFS,
                        }:
                            raise _SchedulerDoctorTestCandidateUnavailable(
                                existing_error
                            ) from existing_error
                        raise
                elif error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    raise _SchedulerDoctorTestCandidateUnavailable(error) from error
                else:
                    raise
        else:
            expected_identity = _scheduler_doctor_test_object_identity(metadata)
            try:
                descriptor = os.open(
                    _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
                    flags,
                    dir_fd=namespace_fd,
                )
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    raise _SchedulerDoctorTestCandidateUnavailable(error) from error
                raise
        descriptor_metadata = _validate_scheduler_doctor_session_lease(
            namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
            descriptor,
            parent_fd=namespace_fd,
        )
        identity = _scheduler_doctor_test_object_identity(descriptor_metadata)
        if expected_identity is not None and expected_identity != identity:
            raise RuntimeError(f"test fixture lease changed while opening: {namespace}")
    except BaseException as primary:
        secondary: list[str] = []
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                secondary.append(str(error))
        if secondary:
            raise RuntimeError(f"{primary}; {'; '.join(secondary)}") from primary
        raise
    try:
        os.close(descriptor)
    except OSError as error:
        raise RuntimeError(f"cannot close fixture lease probe: {error}") from error


def _select_scheduler_doctor_test_namespace(
    candidates: tuple[
        Path
        | _SchedulerDoctorBoundNamespaceCandidate
        | _SchedulerDoctorLinuxStickyFallbackCandidate,
        ...,
    ]
    | None = None,
) -> Path:
    # This test-only fixture guarantees an owner-private namespace and assumes
    # cooperative same-UID users obey flock. It cannot atomically prevent a
    # malicious same-UID replace-at-unlink race and does not claim to do so.
    failures: list[str] = []
    selected_candidates = (
        _scheduler_doctor_test_namespace_candidates()
        if candidates is None
        else candidates
    )
    for candidate_entry in selected_candidates:
        if isinstance(
            candidate_entry,
            _SchedulerDoctorLinuxStickyFallbackCandidate,
        ):
            sticky_binding = _scheduler_doctor_linux_sticky_fallback_binding()
            if sticky_binding is None:
                failures.append(
                    "Linux scheduler-doctor sticky fallback is unavailable"
                )
                continue
            candidate_entry = sticky_binding
        expected_binding = (
            candidate_entry
            if isinstance(
                candidate_entry,
                _SchedulerDoctorBoundNamespaceCandidate,
            )
            else None
        )
        requested_candidate = (
            expected_binding.path
            if expected_binding is not None
            else candidate_entry
        )
        candidate = Path(os.path.realpath(requested_candidate))
        if expected_binding is not None and candidate != requested_candidate:
            raise RuntimeError(
                "scheduler-doctor platform parent changed after binding"
            )
        candidate_fd = -1
        namespace_fd = -1
        created: list[tuple[Path, tuple[int, int, int] | None]] = []
        try:
            if expected_binding is None:
                candidate_fd, candidate_identity, candidate_access_policy = (
                    _bind_scheduler_doctor_test_root(candidate)
                )
            elif _scheduler_doctor_bound_candidate_uses_sticky_root(
                expected_binding
            ):
                candidate_fd, candidate_identity, candidate_access_policy = (
                    _scheduler_doctor_rebind_sticky_candidate(
                        expected_binding
                    )
                )
            else:
                candidate_fd, candidate_identity, candidate_access_policy = (
                    MODULE._bind_mirror_trusted_account_home(candidate)
                )
        except MODULE.SyncError as error:
            if expected_binding is not None:
                raise RuntimeError(
                    "scheduler-doctor platform parent changed after binding"
                ) from error
            if not _scheduler_doctor_test_anchor_is_stably_unsuitable(
                error,
                candidate,
            ):
                raise
            failures.append(f"{candidate}: {error}")
            continue

        if expected_binding is not None and (
            candidate_identity != expected_binding.identity
            or candidate_access_policy != expected_binding.access_policy
        ):
            close_failures = _close_scheduler_doctor_candidate_descriptors(
                (candidate_fd,)
            )
            candidate_fd = -1
            message = "scheduler-doctor platform parent changed after binding"
            if close_failures:
                message += "; " + "; ".join(close_failures)
            raise RuntimeError(message)

        parent = candidate / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME
        namespace = parent / _SCHEDULER_DOCTOR_TEST_NAMESPACE_NAME
        try:
            try:
                parent.mkdir(mode=0o700)
            except FileExistsError:
                _validate_trusted_scheduler_doctor_test_root(parent)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    raise _SchedulerDoctorTestCandidateUnavailable(error) from error
                raise
            else:
                created.append((parent, None))
                metadata = _validate_owner_private_directory(parent)
                created[-1] = (
                    parent,
                    _scheduler_doctor_test_object_identity(metadata),
                )
                _validate_trusted_scheduler_doctor_test_root(parent)
            try:
                namespace.mkdir(mode=0o700)
            except FileExistsError:
                _validate_owner_private_directory(namespace)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    raise _SchedulerDoctorTestCandidateUnavailable(error) from error
                raise
            else:
                created.append((namespace, None))
                metadata = _validate_owner_private_directory(namespace)
                created[-1] = (
                    namespace,
                    _scheduler_doctor_test_object_identity(metadata),
                )
            namespace_fd, _identity, _access_policy = (
                _bind_scheduler_doctor_test_root(namespace)
            )
            _probe_scheduler_doctor_test_namespace_lock(namespace, namespace_fd)
        except _SchedulerDoctorTestCandidateUnavailable as unavailable:
            _close_and_cleanup_scheduler_doctor_candidate(
                unavailable.error,
                (namespace_fd, candidate_fd),
                created,
            )
            failures.append(f"{candidate}: {unavailable.error}")
            continue
        except BaseException as error:
            _close_and_cleanup_scheduler_doctor_candidate(
                error,
                (namespace_fd, candidate_fd),
                created,
            )
            raise
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (namespace_fd, candidate_fd)
        )
        if close_failures:
            raise RuntimeError(
                "cannot close scheduler-doctor namespace probe: "
                + "; ".join(close_failures)
            )
        return namespace
    detail = "; ".join(failures) if failures else "no absolute candidates"
    raise RuntimeError(
        "cannot select an owner-private scheduler-doctor test namespace: " + detail
    )


def _ensure_scheduler_doctor_test_namespace() -> Path:
    global _SCHEDULER_DOCTOR_TEST_NAMESPACE

    if _SCHEDULER_DOCTOR_TEST_NAMESPACE is None:
        _SCHEDULER_DOCTOR_TEST_NAMESPACE = (
            _select_scheduler_doctor_test_namespace()
        )
    _validate_owner_private_directory(_SCHEDULER_DOCTOR_TEST_NAMESPACE)
    _validate_trusted_scheduler_doctor_test_root(
        _SCHEDULER_DOCTOR_TEST_NAMESPACE
    )
    return _SCHEDULER_DOCTOR_TEST_NAMESPACE


def _validate_scheduler_doctor_session_lease(
    path: Path,
    descriptor: int,
    *,
    parent_fd: int | None = None,
) -> os.stat_result:
    descriptor_metadata = os.fstat(descriptor)
    path_metadata = (
        path.lstat()
        if parent_fd is None
        else os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    )
    if not stat.S_ISREG(descriptor_metadata.st_mode):
        raise RuntimeError(f"test fixture lease is not regular: {path}")
    if descriptor_metadata.st_nlink != 1:
        raise RuntimeError(f"test fixture lease link count is not one: {path}")
    if descriptor_metadata.st_uid != os.geteuid():
        raise RuntimeError(f"test fixture lease has the wrong owner: {path}")
    if stat.S_IMODE(descriptor_metadata.st_mode) != 0o600:
        raise RuntimeError(f"test fixture lease is not mode 0600: {path}")
    if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != (
        path_metadata.st_dev,
        path_metadata.st_ino,
    ):
        raise RuntimeError(f"test fixture lease identity changed: {path}")
    return descriptor_metadata


def _validate_scheduler_doctor_liveness_descriptor(
    path: Path,
    descriptor: int,
    *,
    parent_fd: int,
    expected_mount_identity: tuple[int, int | None],
    expected_identity: tuple[int, int, int] | None = None,
) -> os.stat_result:
    descriptor_metadata = os.fstat(descriptor)
    path_metadata = os.stat(
        path.name,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    identity = _scheduler_doctor_test_object_identity(descriptor_metadata)
    if not stat.S_ISREG(descriptor_metadata.st_mode):
        raise RuntimeError(f"test fixture liveness lease is not regular: {path}")
    if descriptor_metadata.st_nlink != 1:
        raise RuntimeError(
            f"test fixture liveness lease link count is not one: {path}"
        )
    if descriptor_metadata.st_uid != os.geteuid():
        raise RuntimeError(
            f"test fixture liveness lease has the wrong owner: {path}"
        )
    if stat.S_IMODE(descriptor_metadata.st_mode) != 0o600:
        raise RuntimeError(
            f"test fixture liveness lease is not mode 0600: {path}"
        )
    if identity != _scheduler_doctor_test_object_identity(path_metadata):
        raise RuntimeError(f"test fixture liveness lease identity changed: {path}")
    if expected_identity is not None and identity != expected_identity:
        raise RuntimeError(f"test fixture liveness lease identity changed: {path}")
    if descriptor_metadata.st_dev != expected_mount_identity[0]:
        raise RuntimeError(
            f"test fixture liveness lease crosses a mount boundary: {path}"
        )
    return descriptor_metadata


def _open_scheduler_doctor_liveness_descriptor(
    session_path: Path,
    session_fd: int,
    *,
    expected_mount_identity: tuple[int, int | None],
    create: bool = False,
    expected_identity: tuple[int, int, int] | None = None,
) -> tuple[int, tuple[int, int, int]]:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(
        _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME,
        flags,
        0o600,
        dir_fd=session_fd,
    )
    path = session_path / _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME
    try:
        metadata = _validate_scheduler_doctor_liveness_descriptor(
            path,
            descriptor,
            parent_fd=session_fd,
            expected_mount_identity=expected_mount_identity,
            expected_identity=expected_identity,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, _scheduler_doctor_test_object_identity(metadata)


def _create_unlocked_scheduler_doctor_liveness_marker(
    session_path: Path,
) -> None:
    session_metadata = _validate_owner_private_directory(session_path)
    session_identity = _scheduler_doctor_test_object_identity(session_metadata)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    namespace_fd = os.open(session_path.parent, flags)
    session_fd = -1
    try:
        mount_identity = _scheduler_doctor_stale_directory_mount_identity(
            namespace_fd
        )
        session_fd = _open_scheduler_doctor_stale_directory(
            namespace_fd,
            session_path.name,
            session_identity,
            expected_mount_identity=mount_identity,
            require_owner_private_directory=True,
        )
        # The helper deliberately leaves an unlocked marker that models a
        # process which exited cleanly before the next sweep.
        descriptor, _identity = _open_scheduler_doctor_liveness_descriptor(
            session_path,
            session_fd,
            expected_mount_identity=mount_identity,
            create=True,
        )
        os.close(descriptor)
    finally:
        if session_fd >= 0:
            os.close(session_fd)
        os.close(namespace_fd)


def _scheduler_doctor_liveness_registry_entries() -> tuple[
    tuple[int, tuple[int, int, int]], ...
]:
    entries: dict[int, tuple[int, int, int]] = {}
    serialized = os.environ.get(
        _SCHEDULER_DOCTOR_TEST_LIVENESS_REGISTRY_ENV
    )
    if serialized is not None:
        try:
            payload = json.loads(serialized)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "scheduler-doctor liveness registry is malformed"
            ) from error
        if not isinstance(payload, list):
            raise RuntimeError("scheduler-doctor liveness registry is malformed")
        for item in payload:
            if not isinstance(item, dict) or set(item) != {
                "fd",
                "dev",
                "ino",
                "type",
            }:
                raise RuntimeError(
                    "scheduler-doctor liveness registry is malformed"
                )
            values = tuple(item[key] for key in ("fd", "dev", "ino", "type"))
            if any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in values
            ):
                raise RuntimeError(
                    "scheduler-doctor liveness registry is malformed"
                )
            descriptor, device, inode, object_type = values
            if descriptor < 0 or descriptor in entries:
                raise RuntimeError(
                    "scheduler-doctor liveness registry is malformed"
                )
            entries[descriptor] = (device, inode, object_type)

    binding = _SCHEDULER_DOCTOR_TEST_SESSION
    if binding is not None:
        prior = entries.get(binding.liveness_descriptor)
        if prior is not None and prior != binding.liveness_identity:
            raise RuntimeError(
                "scheduler-doctor liveness registry descriptor collision"
            )
        entries[binding.liveness_descriptor] = binding.liveness_identity

    validated: list[tuple[int, tuple[int, int, int]]] = []
    for descriptor, expected_identity in sorted(entries.items()):
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise RuntimeError(
                "scheduler-doctor inherited liveness descriptor is unavailable"
            ) from error
        identity = _scheduler_doctor_test_object_identity(metadata)
        if (
            identity != expected_identity
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise RuntimeError(
                "scheduler-doctor inherited liveness descriptor changed"
            )
        validated.append((descriptor, identity))
    return tuple(validated)


def _scheduler_doctor_liveness_registry_json(
    entries: tuple[tuple[int, tuple[int, int, int]], ...],
) -> str:
    return json.dumps(
        [
            {
                "fd": descriptor,
                "dev": identity[0],
                "ino": identity[1],
                "type": identity[2],
            }
            for descriptor, identity in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _scheduler_doctor_child_protected_custody() -> dict[
    int, tuple[int, int, int]
]:
    if _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE is not None:
        raise RuntimeError(
            "scheduler-doctor child launch blocked by retained fixture custody"
        )
    protected: dict[int, tuple[int, int, int]] = {}

    def add(
        role: str,
        descriptor: int | None,
        expected_identity: tuple[int, int, int] | None,
    ) -> None:
        if descriptor is None:
            return
        try:
            identity = _scheduler_doctor_test_object_identity(
                os.fstat(descriptor)
            )
        except OSError as error:
            raise RuntimeError(
                f"scheduler-doctor {role} custody descriptor is unavailable"
            ) from error
        if expected_identity is not None and identity != expected_identity:
            raise RuntimeError(
                f"scheduler-doctor {role} custody descriptor changed"
            )
        prior = protected.get(descriptor)
        if prior is not None and prior != identity:
            raise RuntimeError(
                "scheduler-doctor child custody descriptor collision"
            )
        protected[descriptor] = identity

    binding = _SCHEDULER_DOCTOR_TEST_SESSION
    if binding is not None:
        add("session", binding.descriptor, binding.identity)
        add(
            "namespace",
            binding.namespace_descriptor,
            binding.namespace_identity,
        )
    add(
        "module-lease",
        _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD,
        None,
    )
    return protected


def _reject_scheduler_doctor_child_custody(
    descriptors: tuple[int, ...],
) -> None:
    protected = _scheduler_doctor_child_protected_custody()
    protected_identities = set(protected.values())
    for descriptor in descriptors:
        if descriptor in protected:
            raise RuntimeError(
                "scheduler-doctor child launch attempted to inherit fixture "
                "custody descriptors"
            )
        try:
            identity = _scheduler_doctor_test_object_identity(
                os.fstat(descriptor)
            )
        except OSError as error:
            raise RuntimeError(
                "scheduler-doctor child descriptor is unavailable"
            ) from error
        if identity in protected_identities:
            raise RuntimeError(
                "scheduler-doctor child launch attempted to inherit fixture "
                "custody objects"
            )


def _scheduler_doctor_test_popen(*args: object, **kwargs: object):
    if kwargs.get("close_fds") is False:
        raise RuntimeError(
            "scheduler-doctor child launch requires close_fds=True"
        )
    registry = _scheduler_doctor_liveness_registry_entries()
    inherited = tuple(descriptor for descriptor, _identity in registry)
    supplied = kwargs.get("pass_fds", ())
    try:
        supplied_descriptors = tuple(supplied)  # type: ignore[arg-type]
    except TypeError as error:
        raise RuntimeError("scheduler-doctor child pass_fds is invalid") from error
    if any(
        not isinstance(descriptor, int)
        or isinstance(descriptor, bool)
        or descriptor < 0
        for descriptor in supplied_descriptors
    ):
        raise RuntimeError("scheduler-doctor child pass_fds is invalid")

    _reject_scheduler_doctor_child_custody(inherited)

    environment = kwargs.get("env")
    child_environment = os.environ.copy() if environment is None else dict(environment)
    child_environment[_SCHEDULER_DOCTOR_TEST_LIVENESS_REGISTRY_ENV] = (
        _scheduler_doctor_liveness_registry_json(registry)
    )
    kwargs["env"] = child_environment
    kwargs["close_fds"] = True
    final_descriptors = tuple(
        sorted(set(supplied_descriptors + inherited))
    )
    _reject_scheduler_doctor_child_custody(final_descriptors)
    kwargs["pass_fds"] = final_descriptors
    return _SCHEDULER_DOCTOR_TEST_ORIGINAL_POPEN(*args, **kwargs)


def _install_scheduler_doctor_test_popen_wrapper() -> None:
    global _SCHEDULER_DOCTOR_TEST_POPEN_INSTALLED
    if _SCHEDULER_DOCTOR_TEST_POPEN_INSTALLED:
        return
    if subprocess.Popen is not _SCHEDULER_DOCTOR_TEST_ORIGINAL_POPEN:
        raise RuntimeError("subprocess.Popen changed before fixture setup")
    subprocess.Popen = _scheduler_doctor_test_popen  # type: ignore[assignment]
    _SCHEDULER_DOCTOR_TEST_POPEN_INSTALLED = True


def _restore_scheduler_doctor_test_popen_wrapper() -> None:
    global _SCHEDULER_DOCTOR_TEST_POPEN_INSTALLED
    if not _SCHEDULER_DOCTOR_TEST_POPEN_INSTALLED:
        return
    if subprocess.Popen is not _scheduler_doctor_test_popen:
        raise RuntimeError("subprocess.Popen changed before fixture teardown")
    subprocess.Popen = _SCHEDULER_DOCTOR_TEST_ORIGINAL_POPEN
    _SCHEDULER_DOCTOR_TEST_POPEN_INSTALLED = False


def _acquire_scheduler_doctor_test_session_lease(
    descriptor: int,
    *,
    timeout_seconds: float = _SCHEDULER_DOCTOR_TEST_LEASE_TIMEOUT_SECONDS,
    retry_seconds: float = _SCHEDULER_DOCTOR_TEST_LEASE_RETRY_SECONDS,
) -> None:
    if timeout_seconds < 0:
        raise ValueError("scheduler-doctor fixture lease timeout must be non-negative")
    if retry_seconds <= 0:
        raise ValueError("scheduler-doctor fixture lease retry must be positive")
    deadline = time.monotonic() + timeout_seconds
    busy_errors = {errno.EACCES, errno.EAGAIN}
    if hasattr(errno, "EWOULDBLOCK"):
        busy_errors.add(errno.EWOULDBLOCK)
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in busy_errors:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "timed out acquiring scheduler-doctor fixture lease"
                ) from error
            time.sleep(min(retry_seconds, remaining))


def _bounded_scheduler_doctor_stale_session_names(
    namespace: Path | int,
    *,
    deadline: float | None = None,
) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(namespace) as iterator:
        for entry in iterator:
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError(
                    "scheduler-doctor stale-session cleanup planning timed out"
                )
            name = entry.name
            if not isinstance(name, str):
                raise RuntimeError(
                    "scheduler-doctor fixture entry name is not text"
                )
            if name == _SCHEDULER_DOCTOR_TEST_LOCK_NAME:
                continue
            if len(names) == _SCHEDULER_DOCTOR_TEST_NAMESPACE_ENTRY_LIMIT:
                raise RuntimeError(
                    "too many scheduler-doctor fixture namespace entries"
                )
            if not (
                name.startswith(_SCHEDULER_DOCTOR_TEST_SESSION_PREFIX)
                or name.startswith(_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX)
                or name.startswith(_SCHEDULER_DOCTOR_TEST_DELETE_PREFIX)
            ):
                raise RuntimeError(
                    f"unexpected scheduler-doctor fixture entry: {name}"
                )
            names.append(name)
    names.sort(key=os.fsencode)
    return tuple(names)


def _revalidate_scheduler_doctor_stale_directory_names(
    descriptor: int,
    expected_names: tuple[str, ...],
    *,
    deadline: float,
) -> None:
    observed: list[str] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "scheduler-doctor stale-session cleanup planning timed out"
                )
            if len(observed) == len(expected_names):
                raise RuntimeError(
                    "scheduler-doctor stale-session directory changed during "
                    "cleanup planning"
                )
            name = entry.name
            if not isinstance(name, str):
                raise RuntimeError(
                    "scheduler-doctor stale-session entry name is not text"
                )
            observed.append(name)
    observed.sort(key=os.fsencode)
    if tuple(observed) != expected_names:
        raise RuntimeError(
            "scheduler-doctor stale-session directory changed during cleanup "
            "planning"
        )


def _reserve_scheduler_doctor_stale_cleanup_entry(
    budget: _SchedulerDoctorStaleCleanupBudget,
    *,
    depth: int,
) -> None:
    if time.monotonic() >= budget.deadline:
        raise RuntimeError(
            "scheduler-doctor stale-session cleanup planning timed out"
        )
    if depth > budget.depth_limit:
        raise RuntimeError(
            "scheduler-doctor stale-session cleanup depth limit exceeded"
        )
    if budget.remaining_entries == 0:
        raise RuntimeError(
            "scheduler-doctor stale-session cleanup entry limit exceeded"
        )
    budget.remaining_entries -= 1


def _scheduler_doctor_stale_directory_mount_identity(
    descriptor: int,
) -> tuple[int, int | None]:
    try:
        if sys.platform == _SCHEDULER_DOCTOR_TEST_HOST_PLATFORM:
            return MODULE._directory_mount_identity(descriptor)
        # Some fixture regressions emulate another product platform while
        # still operating on the current host kernel. Mount identity must use
        # the real kernel interface rather than that behavioral emulation.
        with mock.patch.object(
            sys,
            "platform",
            _SCHEDULER_DOCTOR_TEST_HOST_PLATFORM,
        ):
            return MODULE._directory_mount_identity(descriptor)
    except (MODULE.SyncError, OSError) as error:
        raise RuntimeError(
            "cannot verify scheduler-doctor stale-session mount identity"
        ) from error


def _open_scheduler_doctor_stale_directory(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int, int],
    *,
    expected_mount_identity: tuple[int, int | None],
    require_owner_private_directory: bool = False,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        descriptor_metadata = os.fstat(descriptor)
        named_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity_changed = (
            _scheduler_doctor_test_object_identity(descriptor_metadata)
            != expected_identity
            or _scheduler_doctor_test_object_identity(named_metadata)
            != expected_identity
        )
        access_policy_changed = require_owner_private_directory and (
            not _scheduler_doctor_metadata_is_owner_private_directory(
                descriptor_metadata
            )
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                named_metadata
            )
        )
        if identity_changed or access_policy_changed:
            raise RuntimeError(
                f"scheduler-doctor stale-session directory changed: {name}"
            )
        if (
            _scheduler_doctor_stale_directory_mount_identity(descriptor)
            != expected_mount_identity
        ):
            raise RuntimeError(
                "scheduler-doctor stale-session directory crosses a mount "
                f"boundary: {name}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _scheduler_doctor_stale_plan_matches(
    metadata: os.stat_result,
    plan: _SchedulerDoctorStaleEntryPlan,
) -> bool:
    if _scheduler_doctor_test_object_identity(metadata) != plan.identity:
        return False
    if metadata.st_uid != os.geteuid():
        return False
    if not plan.owner_private_directory:
        return True
    return _scheduler_doctor_metadata_is_owner_private_directory(metadata)


def _plan_scheduler_doctor_stale_entry(
    parent_fd: int,
    name: str,
    budget: _SchedulerDoctorStaleCleanupBudget,
    *,
    depth: int,
    root_mount_identity: tuple[int, int | None],
    reserved: bool = False,
    require_owner_private_directory: bool = False,
) -> _SchedulerDoctorStaleEntryPlan:
    if not reserved:
        _reserve_scheduler_doctor_stale_cleanup_entry(budget, depth=depth)
    elif time.monotonic() >= budget.deadline:
        raise RuntimeError(
            "scheduler-doctor stale-session cleanup planning timed out"
        )
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    identity = _scheduler_doctor_test_object_identity(metadata)
    if metadata.st_dev != root_mount_identity[0]:
        raise RuntimeError(
            "scheduler-doctor stale-session entry crosses a mount boundary: "
            f"{name}"
        )
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(
            f"scheduler-doctor stale-session entry has the wrong owner: {name}"
        )
    if require_owner_private_directory and (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError(
            f"scheduler-doctor stale-session root is not an owner-private "
            f"directory: {name}"
        )
    if (
        stat.S_ISLNK(metadata.st_mode)
        or stat.S_ISREG(metadata.st_mode)
        or stat.S_ISFIFO(metadata.st_mode)
        or stat.S_ISSOCK(metadata.st_mode)
    ):
        return _SchedulerDoctorStaleEntryPlan(name, identity, None)
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(
            f"unsupported scheduler-doctor stale-session entry: {name}"
        )

    descriptor = _open_scheduler_doctor_stale_directory(
        parent_fd,
        name,
        identity,
        expected_mount_identity=root_mount_identity,
        require_owner_private_directory=require_owner_private_directory,
    )
    try:
        child_names: list[str] = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                child_name = entry.name
                if not isinstance(child_name, str):
                    raise RuntimeError(
                        "scheduler-doctor stale-session entry name is not text"
                    )
                _reserve_scheduler_doctor_stale_cleanup_entry(
                    budget,
                    depth=depth + 1,
                )
                child_names.append(child_name)
        child_names.sort(key=os.fsencode)
        children = tuple(
            _plan_scheduler_doctor_stale_entry(
                descriptor,
                child_name,
                budget,
                depth=depth + 1,
                root_mount_identity=root_mount_identity,
                reserved=True,
            )
            for child_name in child_names
        )
        _revalidate_scheduler_doctor_stale_directory_names(
            descriptor,
            tuple(child_names),
            deadline=budget.deadline,
        )
        descriptor_metadata = os.fstat(descriptor)
        if (
            _scheduler_doctor_test_object_identity(descriptor_metadata)
            != identity
            or require_owner_private_directory
            and not _scheduler_doctor_metadata_is_owner_private_directory(
                descriptor_metadata
            )
        ):
            raise RuntimeError(
                f"scheduler-doctor stale-session directory changed: {name}"
            )
        if (
            _scheduler_doctor_stale_directory_mount_identity(descriptor)
            != root_mount_identity
        ):
            raise RuntimeError(
                "scheduler-doctor stale-session directory crosses a mount "
                f"boundary: {name}"
            )
        return _SchedulerDoctorStaleEntryPlan(
            name,
            identity,
            children,
            owner_private_directory=require_owner_private_directory,
        )
    finally:
        os.close(descriptor)


def _revalidate_scheduler_doctor_stale_entry_plan(
    parent_fd: int,
    plan: _SchedulerDoctorStaleEntryPlan,
    *,
    deadline: float,
    root_mount_identity: tuple[int, int | None],
) -> None:
    if time.monotonic() >= deadline:
        raise RuntimeError(
            "scheduler-doctor stale-session cleanup planning timed out"
        )
    metadata = os.stat(plan.name, dir_fd=parent_fd, follow_symlinks=False)
    if not _scheduler_doctor_stale_plan_matches(metadata, plan):
        raise RuntimeError(
            f"scheduler-doctor stale-session entry changed: {plan.name}"
        )
    if plan.children is None:
        return
    descriptor = _open_scheduler_doctor_stale_directory(
        parent_fd,
        plan.name,
        plan.identity,
        expected_mount_identity=root_mount_identity,
        require_owner_private_directory=plan.owner_private_directory,
    )
    try:
        expected_names = tuple(child.name for child in plan.children)
        _revalidate_scheduler_doctor_stale_directory_names(
            descriptor,
            expected_names,
            deadline=deadline,
        )
        for child in plan.children:
            _revalidate_scheduler_doctor_stale_entry_plan(
                descriptor,
                child,
                deadline=deadline,
                root_mount_identity=root_mount_identity,
            )
        if (
            _scheduler_doctor_stale_directory_mount_identity(descriptor)
            != root_mount_identity
        ):
            raise RuntimeError(
                "scheduler-doctor stale-session directory crosses a mount "
                f"boundary: {plan.name}"
            )
    finally:
        os.close(descriptor)


def _apply_scheduler_doctor_stale_entry_plan(
    parent_fd: int,
    plan: _SchedulerDoctorStaleEntryPlan,
    *,
    deadline: float,
    root_mount_identity: tuple[int, int | None],
) -> None:
    if time.monotonic() >= deadline:
        raise RuntimeError("scheduler-doctor stale-session cleanup timed out")
    metadata = os.stat(plan.name, dir_fd=parent_fd, follow_symlinks=False)
    if not _scheduler_doctor_stale_plan_matches(metadata, plan):
        raise RuntimeError(
            f"scheduler-doctor stale-session entry changed: {plan.name}"
        )
    if plan.children is None:
        os.unlink(plan.name, dir_fd=parent_fd)
        return

    descriptor = _open_scheduler_doctor_stale_directory(
        parent_fd,
        plan.name,
        plan.identity,
        expected_mount_identity=root_mount_identity,
        require_owner_private_directory=plan.owner_private_directory,
    )
    try:
        for child in plan.children:
            _apply_scheduler_doctor_stale_entry_plan(
                descriptor,
                child,
                deadline=deadline,
                root_mount_identity=root_mount_identity,
            )
        if time.monotonic() >= deadline:
            raise RuntimeError("scheduler-doctor stale-session cleanup timed out")
        descriptor_metadata = os.fstat(descriptor)
        if (
            _scheduler_doctor_test_object_identity(descriptor_metadata)
            != plan.identity
            or plan.owner_private_directory
            and not _scheduler_doctor_metadata_is_owner_private_directory(
                descriptor_metadata
            )
        ):
            raise RuntimeError(
                f"scheduler-doctor stale-session directory changed: {plan.name}"
            )
        if (
            _scheduler_doctor_stale_directory_mount_identity(descriptor)
            != root_mount_identity
        ):
            raise RuntimeError(
                "scheduler-doctor stale-session directory crosses a mount "
                f"boundary: {plan.name}"
            )
        with os.scandir(descriptor) as iterator:
            unexpected = next(iterator, None)
        if unexpected is not None:
            raise RuntimeError(
                "scheduler-doctor stale-session directory changed during cleanup: "
                f"{plan.name}"
            )
    finally:
        os.close(descriptor)
    metadata = os.stat(plan.name, dir_fd=parent_fd, follow_symlinks=False)
    if not _scheduler_doctor_stale_plan_matches(metadata, plan):
        raise RuntimeError(
            f"scheduler-doctor stale-session directory changed: {plan.name}"
        )
    if time.monotonic() >= deadline:
        raise RuntimeError("scheduler-doctor stale-session cleanup timed out")
    os.rmdir(plan.name, dir_fd=parent_fd)


def _scheduler_doctor_liveness_is_busy(descriptor: int) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        busy_errors = {errno.EACCES, errno.EAGAIN}
        if hasattr(errno, "EWOULDBLOCK"):
            busy_errors.add(errno.EWOULDBLOCK)
        if error.errno in busy_errors:
            return True
        raise RuntimeError(
            "cannot prove scheduler-doctor stale-session liveness"
        ) from error
    return False


def _plan_scheduler_doctor_session_contents(
    session_fd: int,
    budget: _SchedulerDoctorStaleCleanupBudget,
    *,
    root_mount_identity: tuple[int, int | None],
    child_depth: int = 2,
) -> tuple[_SchedulerDoctorStaleEntryPlan, ...]:
    child_names: list[str] = []
    marker_seen = False
    with os.scandir(session_fd) as iterator:
        for entry in iterator:
            child_name = entry.name
            if not isinstance(child_name, str):
                raise RuntimeError(
                    "scheduler-doctor stale-session entry name is not text"
                )
            if child_name == _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME:
                if marker_seen:
                    raise RuntimeError(
                        "scheduler-doctor stale-session liveness is ambiguous"
                    )
                marker_seen = True
                continue
            _reserve_scheduler_doctor_stale_cleanup_entry(
                budget,
                depth=child_depth,
            )
            child_names.append(child_name)
    if not marker_seen:
        raise RuntimeError(
            "scheduler-doctor stale-session liveness lease is missing"
        )
    child_names.sort(key=os.fsencode)
    plans = tuple(
        _plan_scheduler_doctor_stale_entry(
            session_fd,
            child_name,
            budget,
            depth=child_depth,
            root_mount_identity=root_mount_identity,
            reserved=True,
        )
        for child_name in child_names
    )
    expected_names = tuple(
        sorted(
            child_names
            + (
                [_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME]
                if marker_seen
                else []
            ),
            key=os.fsencode,
        )
    )
    _revalidate_scheduler_doctor_stale_directory_names(
        session_fd,
        expected_names,
        deadline=budget.deadline,
    )
    return plans


def _validate_scheduler_doctor_delete_quarantine_binding(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
    quarantine: _SchedulerDoctorDeleteQuarantineBinding,
    expected_names: tuple[str, ...],
    *,
    deadline: float,
) -> None:
    descriptor_metadata = os.fstat(quarantine.descriptor)
    named_metadata = os.stat(
        quarantine.name,
        dir_fd=namespace_fd,
        follow_symlinks=False,
    )
    if (
        _scheduler_doctor_test_object_identity(descriptor_metadata)
        != quarantine.identity
        or _scheduler_doctor_test_object_identity(named_metadata)
        != quarantine.identity
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            descriptor_metadata
        )
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            named_metadata
        )
        or _scheduler_doctor_stale_directory_mount_identity(
            quarantine.descriptor
        )
        != namespace_mount_identity
    ):
        raise RuntimeError("scheduler-doctor delete quarantine changed")
    _revalidate_scheduler_doctor_stale_directory_names(
        quarantine.descriptor,
        expected_names,
        deadline=deadline,
    )


def _validate_scheduler_doctor_delete_payload_binding(
    quarantine_fd: int,
    payload_fd: int,
    payload_identity: tuple[int, int, int],
    namespace_mount_identity: tuple[int, int | None],
) -> None:
    descriptor_metadata = os.fstat(payload_fd)
    named_metadata = os.stat(
        _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,
        dir_fd=quarantine_fd,
        follow_symlinks=False,
    )
    if (
        _scheduler_doctor_test_object_identity(descriptor_metadata)
        != payload_identity
        or _scheduler_doctor_test_object_identity(named_metadata)
        != payload_identity
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            descriptor_metadata
        )
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            named_metadata
        )
        or _scheduler_doctor_stale_directory_mount_identity(payload_fd)
        != namespace_mount_identity
    ):
        raise RuntimeError(
            "scheduler-doctor delete quarantine payload changed"
        )


def _create_scheduler_doctor_delete_quarantine(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
) -> _SchedulerDoctorDeleteQuarantineBinding:
    for _attempt in range(_SCHEDULER_DOCTOR_TEST_DELETE_CREATE_ATTEMPTS):
        name = (
            _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX
            + os.urandom(_SCHEDULER_DOCTOR_TEST_DELETE_NONCE_BYTES).hex()
        )
        try:
            os.mkdir(name, 0o700, dir_fd=namespace_fd)
        except FileExistsError:
            continue

        descriptor = -1
        binding: _SchedulerDoctorDeleteQuarantineBinding | None = None
        try:
            metadata = os.stat(
                name,
                dir_fd=namespace_fd,
                follow_symlinks=False,
            )
            identity = _scheduler_doctor_test_object_identity(metadata)
            if not _scheduler_doctor_metadata_is_owner_private_directory(metadata):
                raise RuntimeError(
                    "scheduler-doctor delete quarantine is not an owner-private "
                    f"directory: {name}"
                )
            descriptor = _open_scheduler_doctor_stale_directory(
                namespace_fd,
                name,
                identity,
                expected_mount_identity=namespace_mount_identity,
                require_owner_private_directory=True,
            )
            binding = _SchedulerDoctorDeleteQuarantineBinding(
                name=name,
                identity=identity,
                descriptor=descriptor,
                payload_identity=None,
            )
            _validate_scheduler_doctor_delete_quarantine_binding(
                namespace_fd,
                namespace_mount_identity,
                binding,
                (),
                deadline=(
                    time.monotonic()
                    + _SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_TIMEOUT_SECONDS
                ),
            )
            return binding
        except BaseException as error:
            cleanup_failure: BaseException | None = None
            if binding is None:
                cleanup_failure = RuntimeError(
                    "delete quarantine binding is unavailable"
                )
            else:
                try:
                    _validate_scheduler_doctor_delete_quarantine_binding(
                        namespace_fd,
                        namespace_mount_identity,
                        binding,
                        (),
                        deadline=(
                            time.monotonic()
                            + _SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_TIMEOUT_SECONDS
                        ),
                    )
                    os.rmdir(name, dir_fd=namespace_fd)
                except BaseException as cleanup_error:
                    cleanup_failure = cleanup_error
            close_failures = _close_scheduler_doctor_candidate_descriptors(
                (descriptor,)
            )
            retained = False
            try:
                os.stat(
                    name,
                    dir_fd=namespace_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError:
                retained = True
            else:
                retained = True
            detail = "; ".join(close_failures)
            if cleanup_failure is not None:
                detail = (
                    f"{detail}; " if detail else ""
                ) + f"empty quarantine cleanup failed: {cleanup_failure}"
            message = f"{error}; {detail}" if detail else str(error)
            if retained:
                raise _SchedulerDoctorQuarantineTransitionFailure(
                    name,
                    message,
                ) from error
            if close_failures or cleanup_failure is not None:
                raise RuntimeError(message) from error
            raise
    raise RuntimeError(
        "cannot allocate scheduler-doctor delete quarantine after bounded "
        "nonce retries"
    )


def _remove_empty_scheduler_doctor_delete_quarantine(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
    quarantine: _SchedulerDoctorDeleteQuarantineBinding,
    *,
    deadline: float,
) -> None:
    _validate_scheduler_doctor_delete_quarantine_binding(
        namespace_fd,
        namespace_mount_identity,
        quarantine,
        (),
        deadline=deadline,
    )
    os.rmdir(quarantine.name, dir_fd=namespace_fd)


def _quarantine_scheduler_doctor_session(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
    source_name: str,
    source_fd: int,
    source_identity: tuple[int, int, int],
    *,
    deadline: float,
) -> _SchedulerDoctorDeleteQuarantineBinding:
    quarantine = _create_scheduler_doctor_delete_quarantine(
        namespace_fd,
        namespace_mount_identity,
    )
    renamed = False
    retained_quarantine = False
    try:
        _validate_scheduler_doctor_delete_quarantine_binding(
            namespace_fd,
            namespace_mount_identity,
            quarantine,
            (),
            deadline=deadline,
        )
        source_descriptor_metadata = os.fstat(source_fd)
        source_named_metadata = os.stat(
            source_name,
            dir_fd=namespace_fd,
            follow_symlinks=False,
        )
        if (
            _scheduler_doctor_test_object_identity(source_descriptor_metadata)
            != source_identity
            or _scheduler_doctor_test_object_identity(source_named_metadata)
            != source_identity
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                source_descriptor_metadata
            )
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                source_named_metadata
            )
            or _scheduler_doctor_stale_directory_mount_identity(source_fd)
            != namespace_mount_identity
        ):
            raise RuntimeError(
                "scheduler-doctor session changed before quarantine rename"
            )
        try:
            os.stat(
                _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,
                dir_fd=quarantine.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError(
                "scheduler-doctor delete quarantine payload already exists"
            )
        _validate_scheduler_doctor_delete_quarantine_binding(
            namespace_fd,
            namespace_mount_identity,
            quarantine,
            (),
            deadline=deadline,
        )
        try:
            os.rename(
                source_name,
                _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,
                src_dir_fd=namespace_fd,
                dst_dir_fd=quarantine.descriptor,
            )
            renamed = True
        except BaseException as error:
            try:
                _remove_empty_scheduler_doctor_delete_quarantine(
                    namespace_fd,
                    namespace_mount_identity,
                    quarantine,
                    deadline=deadline,
                )
            except BaseException as cleanup_error:
                retained_quarantine = True
                raise RuntimeError(
                    f"{error}; empty quarantine cleanup failed: {cleanup_error}"
                ) from error
            raise

        try:
            os.stat(
                source_name,
                dir_fd=namespace_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RuntimeError(
                "cannot prove scheduler-doctor source namespace was released"
            ) from error
        else:
            raise RuntimeError(
                "scheduler-doctor source namespace remained after quarantine rename"
            )

        renamed_quarantine = _SchedulerDoctorDeleteQuarantineBinding(
            name=quarantine.name,
            identity=quarantine.identity,
            descriptor=quarantine.descriptor,
            payload_identity=source_identity,
        )
        _validate_scheduler_doctor_delete_quarantine_binding(
            namespace_fd,
            namespace_mount_identity,
            renamed_quarantine,
            (_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,),
            deadline=deadline,
        )
        _validate_scheduler_doctor_delete_payload_binding(
            quarantine.descriptor,
            source_fd,
            source_identity,
            namespace_mount_identity,
        )
        return renamed_quarantine
    except BaseException as error:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (quarantine.descriptor,)
        )
        message = str(error)
        if close_failures:
            message += "; " + "; ".join(close_failures)
        if renamed or retained_quarantine:
            raise _SchedulerDoctorQuarantineTransitionFailure(
                quarantine.name,
                message,
            ) from error
        if close_failures:
            raise RuntimeError(message) from error
        raise


def _classify_scheduler_doctor_delete_quarantine(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
    name: str,
    budget: _SchedulerDoctorStaleCleanupBudget,
) -> _SchedulerDoctorDeleteQuarantineCandidate:
    _reserve_scheduler_doctor_stale_cleanup_entry(budget, depth=1)
    metadata = os.stat(name, dir_fd=namespace_fd, follow_symlinks=False)
    identity = _scheduler_doctor_test_object_identity(metadata)
    if not _scheduler_doctor_metadata_is_owner_private_directory(metadata):
        raise RuntimeError(
            "scheduler-doctor delete quarantine is not an owner-private "
            f"directory: {name}"
        )
    quarantine_fd = _open_scheduler_doctor_stale_directory(
        namespace_fd,
        name,
        identity,
        expected_mount_identity=namespace_mount_identity,
        require_owner_private_directory=True,
    )
    payload_fd = -1
    liveness_fd = -1
    primary: BaseException | None = None
    try:
        quarantine = _SchedulerDoctorDeleteQuarantineBinding(
            name=name,
            identity=identity,
            descriptor=quarantine_fd,
            payload_identity=None,
        )
        try:
            payload_metadata = os.stat(
                _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            _validate_scheduler_doctor_delete_quarantine_binding(
                namespace_fd,
                namespace_mount_identity,
                quarantine,
                (),
                deadline=budget.deadline,
            )
            return _SchedulerDoctorDeleteQuarantineCandidate(
                name=name,
                identity=identity,
                payload_identity=None,
                liveness_identity=None,
                liveness_present=False,
                busy=False,
                plans=(),
            )
        _validate_scheduler_doctor_delete_quarantine_binding(
            namespace_fd,
            namespace_mount_identity,
            quarantine,
            (_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,),
            deadline=budget.deadline,
        )
        _reserve_scheduler_doctor_stale_cleanup_entry(budget, depth=2)
        payload_identity = _scheduler_doctor_test_object_identity(payload_metadata)
        if not _scheduler_doctor_metadata_is_owner_private_directory(
            payload_metadata
        ):
            raise RuntimeError(
                "scheduler-doctor delete quarantine payload is not owner-private"
            )
        payload_fd = _open_scheduler_doctor_stale_directory(
            quarantine_fd,
            _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,
            payload_identity,
            expected_mount_identity=namespace_mount_identity,
            require_owner_private_directory=True,
        )
        _validate_scheduler_doctor_delete_payload_binding(
            quarantine_fd,
            payload_fd,
            payload_identity,
            namespace_mount_identity,
        )
        with os.scandir(payload_fd) as iterator:
            if time.monotonic() >= budget.deadline:
                raise RuntimeError(
                    "scheduler-doctor stale-session cleanup planning timed out"
                )
            first_payload_entry = next(iterator, None)
        if (
            first_payload_entry is not None
            and not isinstance(first_payload_entry.name, str)
        ):
            raise RuntimeError(
                "scheduler-doctor delete quarantine payload name is not text"
            )
        liveness_present = first_payload_entry is not None
        liveness_identity: tuple[int, int, int] | None = None
        busy = False
        plans: tuple[_SchedulerDoctorStaleEntryPlan, ...] | None = None
        if liveness_present:
            try:
                liveness_fd, liveness_identity = (
                    _open_scheduler_doctor_liveness_descriptor(
                        Path(_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME),
                        payload_fd,
                        expected_mount_identity=namespace_mount_identity,
                    )
                )
            except FileNotFoundError as error:
                raise RuntimeError(
                    "scheduler-doctor markerless delete quarantine payload is "
                    "not empty"
                ) from error
            busy = _scheduler_doctor_liveness_is_busy(liveness_fd)
        if not busy and liveness_present:
            plans = _plan_scheduler_doctor_session_contents(
                payload_fd,
                budget,
                root_mount_identity=namespace_mount_identity,
                child_depth=3,
            )
            assert liveness_identity is not None
            _validate_scheduler_doctor_liveness_descriptor(
                Path(_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME),
                liveness_fd,
                parent_fd=payload_fd,
                expected_mount_identity=namespace_mount_identity,
                expected_identity=liveness_identity,
            )
        elif not busy:
            plans = ()
        _validate_scheduler_doctor_delete_quarantine_binding(
            namespace_fd,
            namespace_mount_identity,
            quarantine,
            (_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,),
            deadline=budget.deadline,
        )
        return _SchedulerDoctorDeleteQuarantineCandidate(
            name=name,
            identity=identity,
            payload_identity=payload_identity,
            liveness_identity=liveness_identity,
            liveness_present=liveness_present,
            busy=busy,
            plans=plans,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (liveness_fd, payload_fd, quarantine_fd)
        )
        if close_failures:
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(message)


def _delete_scheduler_doctor_quarantine_payload(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
    candidate: _SchedulerDoctorDeleteQuarantineCandidate,
    *,
    deadline: float,
    quarantine_descriptor: int | None = None,
    payload_descriptor: int | None = None,
    liveness_descriptor: int | None = None,
) -> bool:
    quarantine_fd = -1
    payload_fd = -1
    liveness_fd = -1
    owned_descriptors: list[int] = []
    if quarantine_descriptor is None:
        quarantine_fd = _open_scheduler_doctor_stale_directory(
            namespace_fd,
            candidate.name,
            candidate.identity,
            expected_mount_identity=namespace_mount_identity,
            require_owner_private_directory=True,
        )
        owned_descriptors.append(quarantine_fd)
    else:
        quarantine_fd = quarantine_descriptor
    quarantine = _SchedulerDoctorDeleteQuarantineBinding(
        name=candidate.name,
        identity=candidate.identity,
        descriptor=quarantine_fd,
        payload_identity=candidate.payload_identity,
    )
    primary: BaseException | None = None
    delete_complete = False
    try:
        if candidate.payload_identity is None:
            _validate_scheduler_doctor_delete_quarantine_binding(
                namespace_fd,
                namespace_mount_identity,
                quarantine,
                (),
                deadline=deadline,
            )
        else:
            if candidate.plans is None:
                if candidate.busy:
                    return False
                raise RuntimeError(
                    "scheduler-doctor delete quarantine plan is unavailable"
                )
            _validate_scheduler_doctor_delete_quarantine_binding(
                namespace_fd,
                namespace_mount_identity,
                quarantine,
                (_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,),
                deadline=deadline,
            )
            if payload_descriptor is None:
                payload_fd = _open_scheduler_doctor_stale_directory(
                    quarantine_fd,
                    _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,
                    candidate.payload_identity,
                    expected_mount_identity=namespace_mount_identity,
                    require_owner_private_directory=True,
                )
                owned_descriptors.append(payload_fd)
            else:
                payload_fd = payload_descriptor
            _validate_scheduler_doctor_delete_payload_binding(
                quarantine_fd,
                payload_fd,
                candidate.payload_identity,
                namespace_mount_identity,
            )
            if candidate.liveness_present:
                if candidate.liveness_identity is None:
                    raise RuntimeError(
                        "scheduler-doctor delete quarantine liveness plan is invalid"
                    )
                if liveness_descriptor is None:
                    liveness_fd, liveness_identity = (
                        _open_scheduler_doctor_liveness_descriptor(
                            Path(_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME),
                            payload_fd,
                            expected_mount_identity=namespace_mount_identity,
                            expected_identity=candidate.liveness_identity,
                        )
                    )
                    owned_descriptors.append(liveness_fd)
                else:
                    liveness_fd = liveness_descriptor
                    liveness_identity = candidate.liveness_identity
                if _scheduler_doctor_liveness_is_busy(liveness_fd):
                    return False
                _validate_scheduler_doctor_liveness_descriptor(
                    Path(_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME),
                    liveness_fd,
                    parent_fd=payload_fd,
                    expected_mount_identity=namespace_mount_identity,
                    expected_identity=liveness_identity,
                )
            expected_names = tuple(
                sorted(
                    [plan.name for plan in candidate.plans]
                    + (
                        [_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME]
                        if candidate.liveness_present
                        else []
                    ),
                    key=os.fsencode,
                )
            )
            _revalidate_scheduler_doctor_stale_directory_names(
                payload_fd,
                expected_names,
                deadline=deadline,
            )
            for plan in candidate.plans:
                _revalidate_scheduler_doctor_stale_entry_plan(
                    payload_fd,
                    plan,
                    deadline=deadline,
                    root_mount_identity=namespace_mount_identity,
                )
            _validate_scheduler_doctor_delete_quarantine_binding(
                namespace_fd,
                namespace_mount_identity,
                quarantine,
                (_SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,),
                deadline=deadline,
            )
            _validate_scheduler_doctor_delete_payload_binding(
                quarantine_fd,
                payload_fd,
                candidate.payload_identity,
                namespace_mount_identity,
            )
            for plan in candidate.plans:
                _apply_scheduler_doctor_stale_entry_plan(
                    payload_fd,
                    plan,
                    deadline=deadline,
                    root_mount_identity=namespace_mount_identity,
                )
            if candidate.liveness_present:
                assert candidate.liveness_identity is not None
                _validate_scheduler_doctor_liveness_descriptor(
                    Path(_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME),
                    liveness_fd,
                    parent_fd=payload_fd,
                    expected_mount_identity=namespace_mount_identity,
                    expected_identity=candidate.liveness_identity,
                )
                os.unlink(
                    _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME,
                    dir_fd=payload_fd,
                )
            _revalidate_scheduler_doctor_stale_directory_names(
                payload_fd,
                (),
                deadline=deadline,
            )
            _validate_scheduler_doctor_delete_payload_binding(
                quarantine_fd,
                payload_fd,
                candidate.payload_identity,
                namespace_mount_identity,
            )
            os.rmdir(
                _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME,
                dir_fd=quarantine_fd,
            )
            _validate_scheduler_doctor_delete_quarantine_binding(
                namespace_fd,
                namespace_mount_identity,
                quarantine,
                (),
                deadline=deadline,
            )
        _validate_scheduler_doctor_delete_quarantine_binding(
            namespace_fd,
            namespace_mount_identity,
            quarantine,
            (),
            deadline=deadline,
        )
        os.rmdir(candidate.name, dir_fd=namespace_fd)
        delete_complete = True
    except BaseException as error:
        primary = error
        raise
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            tuple(reversed(owned_descriptors))
        )
        if close_failures:
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(message)
    return delete_complete


def _delete_scheduler_doctor_stale_session(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
    candidate: _SchedulerDoctorStaleSessionCandidate,
    *,
    deadline: float,
) -> bool:
    if candidate.plans is None:
        raise RuntimeError(
            "scheduler-doctor stale-session cleanup plan is unavailable"
        )
    session_fd = _open_scheduler_doctor_stale_directory(
        namespace_fd,
        candidate.name,
        candidate.identity,
        expected_mount_identity=namespace_mount_identity,
        require_owner_private_directory=True,
    )
    liveness_fd = -1
    quarantine: _SchedulerDoctorDeleteQuarantineBinding | None = None
    delete_result = False
    primary: BaseException | None = None
    try:
        if candidate.liveness_identity is None:
            if not candidate.staging or candidate.plans:
                raise RuntimeError(
                    "scheduler-doctor stale-session liveness plan is invalid"
                )
            _revalidate_scheduler_doctor_stale_directory_names(
                session_fd,
                (),
                deadline=deadline,
            )
            if (
                _scheduler_doctor_stale_directory_mount_identity(session_fd)
                != namespace_mount_identity
            ):
                raise RuntimeError(
                    "scheduler-doctor staging directory crosses a mount "
                    f"boundary: {candidate.name}"
                )
            metadata = os.stat(
                candidate.name,
                dir_fd=namespace_fd,
                follow_symlinks=False,
            )
            if (
                _scheduler_doctor_test_object_identity(metadata)
                != candidate.identity
                or not _scheduler_doctor_metadata_is_owner_private_directory(
                    metadata
                )
            ):
                raise RuntimeError(
                    "scheduler-doctor staging directory changed: "
                    f"{candidate.name}"
                )
            quarantine = _quarantine_scheduler_doctor_session(
                namespace_fd,
                namespace_mount_identity,
                candidate.name,
                session_fd,
                candidate.identity,
                deadline=deadline,
            )
            delete_result = _delete_scheduler_doctor_quarantine_payload(
                namespace_fd,
                namespace_mount_identity,
                _SchedulerDoctorDeleteQuarantineCandidate(
                    name=quarantine.name,
                    identity=quarantine.identity,
                    payload_identity=quarantine.payload_identity,
                    liveness_identity=None,
                    liveness_present=False,
                    busy=False,
                    plans=candidate.plans,
                ),
                deadline=deadline,
                quarantine_descriptor=quarantine.descriptor,
                payload_descriptor=session_fd,
            )
        else:
            liveness_fd, liveness_identity = (
                _open_scheduler_doctor_liveness_descriptor(
                    Path(candidate.name),
                    session_fd,
                    expected_mount_identity=namespace_mount_identity,
                    expected_identity=candidate.liveness_identity,
                )
            )
            if _scheduler_doctor_liveness_is_busy(liveness_fd):
                return False
            expected_names = tuple(
                sorted(
                    [plan.name for plan in candidate.plans]
                    + [_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME],
                    key=os.fsencode,
                )
            )
            _revalidate_scheduler_doctor_stale_directory_names(
                session_fd,
                expected_names,
                deadline=deadline,
            )
            if (
                _scheduler_doctor_stale_directory_mount_identity(session_fd)
                != namespace_mount_identity
            ):
                raise RuntimeError(
                    "scheduler-doctor stale-session directory crosses a mount "
                    f"boundary: {candidate.name}"
                )
            for plan in candidate.plans:
                _revalidate_scheduler_doctor_stale_entry_plan(
                    session_fd,
                    plan,
                    deadline=deadline,
                    root_mount_identity=namespace_mount_identity,
                )
            _validate_scheduler_doctor_liveness_descriptor(
                Path(_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME),
                liveness_fd,
                parent_fd=session_fd,
                expected_mount_identity=namespace_mount_identity,
                expected_identity=liveness_identity,
            )
            quarantine = _quarantine_scheduler_doctor_session(
                namespace_fd,
                namespace_mount_identity,
                candidate.name,
                session_fd,
                candidate.identity,
                deadline=deadline,
            )
            delete_result = _delete_scheduler_doctor_quarantine_payload(
                namespace_fd,
                namespace_mount_identity,
                _SchedulerDoctorDeleteQuarantineCandidate(
                    name=quarantine.name,
                    identity=quarantine.identity,
                    payload_identity=quarantine.payload_identity,
                    liveness_identity=candidate.liveness_identity,
                    liveness_present=True,
                    busy=False,
                    plans=candidate.plans,
                ),
                deadline=deadline,
                quarantine_descriptor=quarantine.descriptor,
                payload_descriptor=session_fd,
                liveness_descriptor=liveness_fd,
            )
    except BaseException as error:
        primary = error
        raise
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (
                liveness_fd,
                session_fd,
                quarantine.descriptor if quarantine is not None else -1,
            )
        )
        if close_failures:
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(message)
    return delete_result


def _classify_scheduler_doctor_stale_session(
    namespace_fd: int,
    namespace_mount_identity: tuple[int, int | None],
    name: str,
    budget: _SchedulerDoctorStaleCleanupBudget,
) -> _SchedulerDoctorStaleSessionCandidate:
    staging = name.startswith(_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX)
    metadata = os.stat(name, dir_fd=namespace_fd, follow_symlinks=False)
    identity = _scheduler_doctor_test_object_identity(metadata)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not _scheduler_doctor_metadata_is_owner_private_directory(metadata)
    ):
        raise RuntimeError(
            "scheduler-doctor stale-session root is not an owner-private "
            f"directory: {name}"
        )
    session_fd = _open_scheduler_doctor_stale_directory(
        namespace_fd,
        name,
        identity,
        expected_mount_identity=namespace_mount_identity,
        require_owner_private_directory=True,
    )
    liveness_fd = -1
    primary: BaseException | None = None
    try:
        if time.monotonic() >= budget.deadline:
            raise RuntimeError(
                "scheduler-doctor stale-session cleanup planning timed out"
            )
        with os.scandir(session_fd) as iterator:
            first_entry = next(iterator, None)
        if staging and first_entry is None:
            _reserve_scheduler_doctor_stale_cleanup_entry(budget, depth=1)
            if (
                _scheduler_doctor_stale_directory_mount_identity(session_fd)
                != namespace_mount_identity
            ):
                raise RuntimeError(
                    "scheduler-doctor staging directory crosses a mount "
                    f"boundary: {name}"
                )
            return _SchedulerDoctorStaleSessionCandidate(
                name,
                identity,
                None,
                False,
                (),
                True,
            )
        liveness_fd, liveness_identity = (
            _open_scheduler_doctor_liveness_descriptor(
            Path(name),
            session_fd,
            expected_mount_identity=namespace_mount_identity,
        )
        )
        busy = _scheduler_doctor_liveness_is_busy(liveness_fd)
        plans: tuple[_SchedulerDoctorStaleEntryPlan, ...] | None = None
        if not busy:
            _reserve_scheduler_doctor_stale_cleanup_entry(budget, depth=1)
            plans = _plan_scheduler_doctor_session_contents(
                session_fd,
                budget,
                root_mount_identity=namespace_mount_identity,
            )
            if staging and plans:
                raise RuntimeError(
                    "scheduler-doctor staging directory retained unexpected "
                    f"entries: {name}"
                )
            for plan in plans:
                _revalidate_scheduler_doctor_stale_entry_plan(
                    session_fd,
                    plan,
                    deadline=budget.deadline,
                    root_mount_identity=namespace_mount_identity,
                )
            _validate_scheduler_doctor_liveness_descriptor(
                Path(_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME),
                liveness_fd,
                parent_fd=session_fd,
                expected_mount_identity=namespace_mount_identity,
                expected_identity=liveness_identity,
            )
    except BaseException as error:
        primary = error
        raise
    finally:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (liveness_fd, session_fd)
        )
        if close_failures:
            message = "; ".join(close_failures)
            if primary is not None:
                raise RuntimeError(f"{primary}; {message}") from primary
            raise RuntimeError(message)
    return _SchedulerDoctorStaleSessionCandidate(
        name,
        identity,
        liveness_identity,
        busy,
        plans,
        staging,
    )


def _sweep_stale_scheduler_doctor_sessions(namespace: Path) -> None:
    # The protected property is the identity of every planned name object and
    # its current-UID ownership, the owner-private access policy of the
    # namespace/session roots, and confinement to the namespace's exact mount.
    # Benign timestamps and nested-file mode changes are not treated as
    # replacement.
    # The module lease serializes cooperative same-UID test processes; this
    # fixture does not claim to defeat a malicious same-UID replace-at-unlink.
    if _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE is not None:
        failure = _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE
        raise RuntimeError(
            "scheduler-doctor active-session cleanup previously failed; "
            f"stale sweep blocked for retained path: {failure.retained_path}"
        )
    namespace_metadata = _validate_owner_private_directory(namespace)
    namespace_identity = _scheduler_doctor_test_object_identity(
        namespace_metadata
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    namespace_fd = os.open(namespace, flags)
    try:
        descriptor_metadata = os.fstat(namespace_fd)
        named_metadata = namespace.lstat()
        if (
            _scheduler_doctor_test_object_identity(descriptor_metadata)
            != namespace_identity
            or _scheduler_doctor_test_object_identity(named_metadata)
            != namespace_identity
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                descriptor_metadata
            )
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                named_metadata
            )
        ):
            raise RuntimeError(
                "scheduler-doctor fixture namespace changed while opening"
            )
        namespace_mount_identity = (
            _scheduler_doctor_stale_directory_mount_identity(namespace_fd)
        )
        deadline = (
            time.monotonic()
            + _SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_TIMEOUT_SECONDS
        )
        session_names = _bounded_scheduler_doctor_stale_session_names(
            namespace_fd,
            deadline=deadline,
        )
        budget = _SchedulerDoctorStaleCleanupBudget(
            deadline=deadline,
            remaining_entries=_SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_ENTRY_LIMIT,
            depth_limit=_SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_DEPTH_LIMIT,
        )
        session_candidates: list[_SchedulerDoctorStaleSessionCandidate] = []
        quarantine_candidates: list[
            _SchedulerDoctorDeleteQuarantineCandidate
        ] = []
        for name in session_names:
            if name.startswith(_SCHEDULER_DOCTOR_TEST_DELETE_PREFIX):
                quarantine_candidates.append(
                    _classify_scheduler_doctor_delete_quarantine(
                        namespace_fd,
                        namespace_mount_identity,
                        name,
                        budget,
                    )
                )
            else:
                session_candidates.append(
                    _classify_scheduler_doctor_stale_session(
                        namespace_fd,
                        namespace_mount_identity,
                        name,
                        budget,
                    )
                )
        if (
            _bounded_scheduler_doctor_stale_session_names(
                namespace_fd,
                deadline=deadline,
            )
            != session_names
        ):
            raise RuntimeError(
                "scheduler-doctor fixture namespace changed during cleanup planning"
            )
        descriptor_metadata = os.fstat(namespace_fd)
        named_metadata = namespace.lstat()
        if (
            _scheduler_doctor_test_object_identity(descriptor_metadata)
            != namespace_identity
            or _scheduler_doctor_test_object_identity(named_metadata)
            != namespace_identity
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                descriptor_metadata
            )
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                named_metadata
            )
            or _scheduler_doctor_stale_directory_mount_identity(namespace_fd)
            != namespace_mount_identity
        ):
            raise RuntimeError(
                "scheduler-doctor fixture namespace changed during cleanup planning"
            )
        retained: set[str] = set()
        for candidate in quarantine_candidates:
            if candidate.busy:
                retained.add(candidate.name)
                continue
            if not _delete_scheduler_doctor_quarantine_payload(
                namespace_fd,
                namespace_mount_identity,
                candidate,
                deadline=deadline,
            ):
                retained.add(candidate.name)
        for candidate in session_candidates:
            if candidate.busy:
                retained.add(candidate.name)
                continue
            if not _delete_scheduler_doctor_stale_session(
                namespace_fd,
                namespace_mount_identity,
                candidate,
                deadline=deadline,
            ):
                retained.add(candidate.name)
        remaining = _bounded_scheduler_doctor_stale_session_names(
            namespace_fd,
            deadline=deadline,
        )
        if set(remaining) != retained or len(remaining) != len(retained):
            raise RuntimeError(
                "scheduler-doctor fixture namespace changed during stale-session "
                "cleanup"
            )
        descriptor_metadata = os.fstat(namespace_fd)
        named_metadata = namespace.lstat()
        if (
            _scheduler_doctor_test_object_identity(descriptor_metadata)
            != namespace_identity
            or _scheduler_doctor_test_object_identity(named_metadata)
            != namespace_identity
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                descriptor_metadata
            )
            or not _scheduler_doctor_metadata_is_owner_private_directory(
                named_metadata
            )
            or _scheduler_doctor_stale_directory_mount_identity(namespace_fd)
            != namespace_mount_identity
        ):
            raise RuntimeError(
                "scheduler-doctor fixture namespace changed during cleanup"
            )
    finally:
        os.close(namespace_fd)


def _resolve_scheduler_doctor_initialization_session_path(
    namespace: Path,
    namespace_descriptor: int,
    session_descriptor: int,
    expected_mount_identity: tuple[int, int | None],
    expected_identity: tuple[int, int, int],
    staging_name: str,
    final_name: str,
) -> Path:
    descriptor_metadata = os.fstat(session_descriptor)
    if (
        _scheduler_doctor_test_object_identity(descriptor_metadata)
        != expected_identity
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            descriptor_metadata
        )
        or _scheduler_doctor_stale_directory_mount_identity(session_descriptor)
        != expected_mount_identity
    ):
        raise RuntimeError(
            "scheduler-doctor initialization session descriptor changed"
        )
    matches: list[str] = []
    for name in (staging_name, final_name):
        try:
            metadata = os.stat(
                name,
                dir_fd=namespace_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(
                "scheduler-doctor initialization session name is unreadable: "
                f"{name}"
            ) from error
        if (
            _scheduler_doctor_test_object_identity(metadata) != expected_identity
            or not _scheduler_doctor_metadata_is_owner_private_directory(metadata)
        ):
            raise RuntimeError(
                "scheduler-doctor initialization session name changed: "
                f"{name}"
            )
        candidate_descriptor = _open_scheduler_doctor_stale_directory(
            namespace_descriptor,
            name,
            expected_identity,
            expected_mount_identity=expected_mount_identity,
            require_owner_private_directory=True,
        )
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (candidate_descriptor,)
        )
        if close_failures:
            raise RuntimeError("; ".join(close_failures))
        matches.append(name)
    if len(matches) != 1:
        raise RuntimeError(
            "scheduler-doctor initialization session publication is ambiguous"
        )
    return namespace / matches[0]


def _cleanup_scheduler_doctor_initialization_path(
    path: Path,
    namespace_descriptor: int,
    session_descriptor: int,
    expected_mount_identity: tuple[int, int | None],
    expected_identity: tuple[int, int, int] | None,
    liveness_descriptor: int,
    expected_liveness_identity: tuple[int, int, int] | None,
) -> None:
    if (
        namespace_descriptor < 0
        or session_descriptor < 0
        or expected_identity is None
    ):
        raise RuntimeError(
            "scheduler-doctor initialization descriptor custody is unavailable"
        )
    metadata = os.stat(
        path.name,
        dir_fd=namespace_descriptor,
        follow_symlinks=False,
    )
    descriptor_metadata = os.fstat(session_descriptor)
    if (
        _scheduler_doctor_test_object_identity(metadata) != expected_identity
        or _scheduler_doctor_test_object_identity(descriptor_metadata)
        != expected_identity
        or not _scheduler_doctor_metadata_is_owner_private_directory(metadata)
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            descriptor_metadata
        )
        or _scheduler_doctor_stale_directory_mount_identity(session_descriptor)
        != expected_mount_identity
    ):
        raise RuntimeError(
            "scheduler-doctor initialization path changed before rollback"
        )
    liveness_present = expected_liveness_identity is not None
    if not liveness_present:
        with os.scandir(session_descriptor) as iterator:
            unexpected = next(iterator, None)
        if unexpected is not None:
            raise RuntimeError(
                "scheduler-doctor initialization path retained unproved entries"
            )
    else:
        if liveness_descriptor < 0:
            raise RuntimeError(
                "scheduler-doctor initialization liveness custody is unavailable"
            )
        _validate_scheduler_doctor_liveness_descriptor(
            path / _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME,
            liveness_descriptor,
            parent_fd=session_descriptor,
            expected_mount_identity=expected_mount_identity,
            expected_identity=expected_liveness_identity,
        )
        _revalidate_scheduler_doctor_stale_directory_names(
            session_descriptor,
            (_SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME,),
            deadline=(
                time.monotonic()
                + _SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_TIMEOUT_SECONDS
            ),
        )
        if _scheduler_doctor_liveness_is_busy(liveness_descriptor):
            raise RuntimeError(
                "scheduler-doctor initialization path is still held by a child"
            )
    deadline = (
        time.monotonic()
        + _SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_TIMEOUT_SECONDS
    )
    quarantine: _SchedulerDoctorDeleteQuarantineBinding | None = None
    try:
        quarantine = _quarantine_scheduler_doctor_session(
            namespace_descriptor,
            expected_mount_identity,
            path.name,
            session_descriptor,
            expected_identity,
            deadline=deadline,
        )
        deleted = _delete_scheduler_doctor_quarantine_payload(
            namespace_descriptor,
            expected_mount_identity,
            _SchedulerDoctorDeleteQuarantineCandidate(
                name=quarantine.name,
                identity=quarantine.identity,
                payload_identity=quarantine.payload_identity,
                liveness_identity=expected_liveness_identity,
                liveness_present=liveness_present,
                busy=False,
                plans=(),
            ),
            deadline=deadline,
            quarantine_descriptor=quarantine.descriptor,
            payload_descriptor=session_descriptor,
            liveness_descriptor=(
                liveness_descriptor if liveness_present else None
            ),
        )
        if not deleted:
            raise RuntimeError(
                "scheduler-doctor initialization path remained busy after "
                "quarantine"
            )
    except BaseException as error:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (quarantine.descriptor,) if quarantine is not None else ()
        )
        message = str(error)
        if close_failures:
            message += "; " + "; ".join(close_failures)
        retained_name = (
            quarantine.name
            if quarantine is not None
            else (
                error.retained_name
                if isinstance(
                    error,
                    _SchedulerDoctorQuarantineTransitionFailure,
                )
                else None
            )
        )
        if retained_name is not None:
            raise _SchedulerDoctorDeleteQuarantineFailure(
                path.parent / retained_name,
                message,
            ) from error
        if close_failures:
            raise RuntimeError(message) from error
        raise
    close_failures = _close_scheduler_doctor_candidate_descriptors(
        (quarantine.descriptor,)
    )
    if close_failures:
        raise _SchedulerDoctorDeleteQuarantineFailure(
            None,
            "; ".join(close_failures),
        )


def _scheduler_doctor_test_session_directory() -> Path:
    global _SCHEDULER_DOCTOR_TEST_SESSION
    global _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD
    global _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE

    if _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE is not None:
        retained_path = (
            _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE.retained_path
        )
        reason = _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE.reason
        raise RuntimeError(
            "scheduler-doctor active-session cleanup previously failed; "
            f"retained for inspection: {retained_path}: {reason}"
        )
    if _SCHEDULER_DOCTOR_TEST_SESSION is not None:
        return _SCHEDULER_DOCTOR_TEST_SESSION.path

    namespace = _ensure_scheduler_doctor_test_namespace()
    lease_path = namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lease_descriptor = os.open(lease_path, flags, 0o600)
    namespace_descriptor = -1
    session_descriptor = -1
    liveness_descriptor = -1
    lease_acquired = False
    session_path: Path | None = None
    session_identity: tuple[int, int, int] | None = None
    liveness_identity: tuple[int, int, int] | None = None
    try:
        _acquire_scheduler_doctor_test_session_lease(lease_descriptor)
        lease_acquired = True
        _validate_scheduler_doctor_session_lease(lease_path, lease_descriptor)
        _sweep_stale_scheduler_doctor_sessions(namespace)
        (
            namespace_descriptor,
            namespace_identity,
            _namespace_access_policy,
        ) = _bind_scheduler_doctor_test_root(namespace)
        namespace_mount_identity = (
            _scheduler_doctor_stale_directory_mount_identity(
                namespace_descriptor
            )
        )
        session_path = Path(
            tempfile.mkdtemp(
                prefix=_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX,
                dir=namespace,
            )
        )
        session_name = session_path.name
        session_descriptor = _open_scheduler_doctor_stale_directory(
            namespace_descriptor,
            session_name,
            _scheduler_doctor_test_object_identity(
                _validate_owner_private_directory(session_path)
            ),
            expected_mount_identity=namespace_mount_identity,
            require_owner_private_directory=True,
        )
        session_metadata = os.fstat(session_descriptor)
        session_identity = _scheduler_doctor_test_object_identity(
            session_metadata
        )
        (
            liveness_descriptor,
            liveness_identity,
        ) = _open_scheduler_doctor_liveness_descriptor(
            session_path,
            session_descriptor,
            expected_mount_identity=namespace_mount_identity,
            create=True,
        )
        fcntl.flock(liveness_descriptor, fcntl.LOCK_SH)
        session_binding = _SchedulerDoctorActiveSessionBinding(
            path=session_path,
            namespace_path=namespace,
            namespace_descriptor=namespace_descriptor,
            namespace_identity=namespace_identity,
            namespace_mount_identity=namespace_mount_identity,
            descriptor=session_descriptor,
            identity=_scheduler_doctor_test_object_identity(session_metadata),
            mount_identity=_scheduler_doctor_stale_directory_mount_identity(
                session_descriptor
            ),
            liveness_descriptor=liveness_descriptor,
            liveness_identity=liveness_identity,
        )
        _validate_scheduler_doctor_active_session_binding(session_binding)
        final_name = (
            _SCHEDULER_DOCTOR_TEST_SESSION_PREFIX
            + session_path.name[len(_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX) :]
        )
        try:
            os.stat(
                final_name,
                dir_fd=namespace_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError(
                "scheduler-doctor published session path already exists"
            )
        staging_name = session_path.name
        try:
            os.rename(
                staging_name,
                final_name,
                src_dir_fd=namespace_descriptor,
                dst_dir_fd=namespace_descriptor,
            )
        except BaseException as rename_error:
            try:
                session_path = (
                    _resolve_scheduler_doctor_initialization_session_path(
                        namespace,
                        namespace_descriptor,
                        session_descriptor,
                        namespace_mount_identity,
                        session_identity,
                        staging_name,
                        final_name,
                    )
                )
            except BaseException as locator_error:
                session_path = None
                raise RuntimeError(
                    f"{rename_error}; initialization publication locator "
                    f"failed: {locator_error}"
                ) from rename_error
            raise
        session_path = namespace / final_name
        session_binding = _SchedulerDoctorActiveSessionBinding(
            path=session_path,
            namespace_path=namespace,
            namespace_descriptor=namespace_descriptor,
            namespace_identity=namespace_identity,
            namespace_mount_identity=namespace_mount_identity,
            descriptor=session_descriptor,
            identity=session_identity,
            mount_identity=_scheduler_doctor_stale_directory_mount_identity(
                session_descriptor
            ),
            liveness_descriptor=liveness_descriptor,
            liveness_identity=liveness_identity,
        )
        _validate_scheduler_doctor_active_session_binding(session_binding)
    except BaseException as error:
        if lease_acquired:
            rollback_custody: list[
                tuple[str, int, tuple[int, int, int] | None]
            ] = []
            for role, descriptor in (
                ("liveness", liveness_descriptor),
                ("session", session_descriptor),
                ("namespace", namespace_descriptor),
                ("module-lease", lease_descriptor),
            ):
                if descriptor < 0:
                    continue
                try:
                    identity = _scheduler_doctor_test_object_identity(
                        os.fstat(descriptor)
                    )
                except OSError:
                    identity = None
                rollback_custody.append((role, descriptor, identity))
            retained_path = session_path if session_path is not None else namespace
            _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
                _SchedulerDoctorSessionCleanupFailure(
                    retained_path=retained_path,
                    reason="initialization rollback in progress",
                    abandoned_custody=tuple(
                        _SchedulerDoctorAbandonedDescriptorCustody(
                            role,
                            descriptor,
                            identity,
                            "retained-open",
                        )
                        for role, descriptor, identity in rollback_custody
                    ),
                )
            )
            if session_path is not None:
                try:
                    _cleanup_scheduler_doctor_initialization_path(
                        session_path,
                        namespace_descriptor,
                        session_descriptor,
                        namespace_mount_identity,
                        session_identity,
                        liveness_descriptor,
                        liveness_identity,
                    )
                except BaseException as cleanup_error:
                    message = f"{error}; rollback cleanup failed: {cleanup_error}"
                    cleanup_retained_path = (
                        cleanup_error.retained_path
                        if isinstance(
                            cleanup_error,
                            _SchedulerDoctorDeleteQuarantineFailure,
                        )
                        else session_path
                    )
                    _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
                        _SchedulerDoctorSessionCleanupFailure(
                            retained_path=cleanup_retained_path,
                            reason=message,
                            abandoned_custody=tuple(
                                _SchedulerDoctorAbandonedDescriptorCustody(
                                    role,
                                    descriptor,
                                    identity,
                                    "retained-open",
                                )
                                for role, descriptor, identity in rollback_custody
                            ),
                        )
                    )
                    raise RuntimeError(message) from error
                retained_path = None
            close_failures, abandoned = (
                _close_scheduler_doctor_cleanup_custody(
                    tuple(rollback_custody)
                )
            )
            if close_failures:
                message = f"{error}; {'; '.join(close_failures)}"
                _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
                    _SchedulerDoctorSessionCleanupFailure(
                        retained_path=retained_path,
                        reason=message,
                        abandoned_custody=abandoned,
                    )
                )
                raise RuntimeError(message) from error
            _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = None
        else:
            close_failures = _close_scheduler_doctor_candidate_descriptors(
                (lease_descriptor,)
            )
            if close_failures:
                raise RuntimeError(
                    f"{error}; {'; '.join(close_failures)}"
                ) from error
        raise

    _SCHEDULER_DOCTOR_TEST_SESSION = session_binding
    _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD = lease_descriptor
    return session_binding.path


def _validate_scheduler_doctor_active_session_binding(
    binding: _SchedulerDoctorActiveSessionBinding,
) -> None:
    try:
        named_namespace_metadata = binding.namespace_path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(
            "scheduler-doctor fixture namespace is missing during active-session "
            "cleanup"
        ) from error
    except OSError as error:
        raise RuntimeError(
            "scheduler-doctor fixture namespace is unreadable during "
            "active-session cleanup"
        ) from error
    descriptor_namespace_metadata = os.fstat(binding.namespace_descriptor)
    if (
        _scheduler_doctor_test_object_identity(named_namespace_metadata)
        != binding.namespace_identity
        or _scheduler_doctor_test_object_identity(descriptor_namespace_metadata)
        != binding.namespace_identity
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            named_namespace_metadata
        )
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            descriptor_namespace_metadata
        )
        or _scheduler_doctor_stale_directory_mount_identity(
            binding.namespace_descriptor
        )
        != binding.namespace_mount_identity
    ):
        raise RuntimeError(
            "scheduler-doctor fixture namespace object changed during "
            "active-session cleanup"
        )

    try:
        named_session_metadata = os.stat(
            binding.path.name,
            dir_fd=binding.namespace_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "scheduler-doctor active session is missing during cleanup"
        ) from error
    except OSError as error:
        raise RuntimeError(
            "scheduler-doctor active session is unreadable during cleanup"
        ) from error
    descriptor_session_metadata = os.fstat(binding.descriptor)
    if (
        _scheduler_doctor_test_object_identity(named_session_metadata)
        != binding.identity
        or _scheduler_doctor_test_object_identity(descriptor_session_metadata)
        != binding.identity
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            named_session_metadata
        )
        or not _scheduler_doctor_metadata_is_owner_private_directory(
            descriptor_session_metadata
        )
        or _scheduler_doctor_stale_directory_mount_identity(binding.descriptor)
        != binding.mount_identity
        or binding.mount_identity != binding.namespace_mount_identity
    ):
        raise RuntimeError(
            "scheduler-doctor active session object changed during cleanup"
        )
    _validate_scheduler_doctor_liveness_descriptor(
        binding.path / _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME,
        binding.liveness_descriptor,
        parent_fd=binding.descriptor,
        expected_mount_identity=binding.mount_identity,
        expected_identity=binding.liveness_identity,
    )


def _remove_bound_scheduler_doctor_active_session(
    binding: _SchedulerDoctorActiveSessionBinding,
    liveness_probe: int,
) -> None:
    # The held module lease serializes cooperative same-UID test processes.
    # As with stale-session cleanup, this fixture does not claim to defeat a
    # malicious same-UID rename in the final stat-to-rmdir instruction gap.
    probe_binding = _SchedulerDoctorActiveSessionBinding(
        path=binding.path,
        namespace_path=binding.namespace_path,
        namespace_descriptor=binding.namespace_descriptor,
        namespace_identity=binding.namespace_identity,
        namespace_mount_identity=binding.namespace_mount_identity,
        descriptor=binding.descriptor,
        identity=binding.identity,
        mount_identity=binding.mount_identity,
        liveness_descriptor=liveness_probe,
        liveness_identity=binding.liveness_identity,
    )
    _validate_scheduler_doctor_active_session_binding(probe_binding)
    deadline = (
        time.monotonic()
        + _SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_TIMEOUT_SECONDS
    )
    budget = _SchedulerDoctorStaleCleanupBudget(
        deadline=deadline,
        remaining_entries=_SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_ENTRY_LIMIT,
        depth_limit=_SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_DEPTH_LIMIT,
    )
    _reserve_scheduler_doctor_stale_cleanup_entry(budget, depth=1)
    plans = _plan_scheduler_doctor_session_contents(
        binding.descriptor,
        budget,
        root_mount_identity=binding.namespace_mount_identity,
    )
    for plan in plans:
        _revalidate_scheduler_doctor_stale_entry_plan(
            binding.descriptor,
            plan,
            deadline=deadline,
            root_mount_identity=binding.namespace_mount_identity,
        )
    _validate_scheduler_doctor_active_session_binding(probe_binding)
    quarantine: _SchedulerDoctorDeleteQuarantineBinding | None = None
    try:
        quarantine = _quarantine_scheduler_doctor_session(
            binding.namespace_descriptor,
            binding.namespace_mount_identity,
            binding.path.name,
            binding.descriptor,
            binding.identity,
            deadline=deadline,
        )
        deleted = _delete_scheduler_doctor_quarantine_payload(
            binding.namespace_descriptor,
            binding.namespace_mount_identity,
            _SchedulerDoctorDeleteQuarantineCandidate(
                name=quarantine.name,
                identity=quarantine.identity,
                payload_identity=quarantine.payload_identity,
                liveness_identity=binding.liveness_identity,
                liveness_present=True,
                busy=False,
                plans=plans,
            ),
            deadline=deadline,
            quarantine_descriptor=quarantine.descriptor,
            payload_descriptor=binding.descriptor,
            liveness_descriptor=liveness_probe,
        )
        if not deleted:
            raise RuntimeError(
                "scheduler-doctor active session remained busy after quarantine"
            )
    except BaseException as error:
        close_failures = _close_scheduler_doctor_candidate_descriptors(
            (quarantine.descriptor,) if quarantine is not None else ()
        )
        message = str(error)
        if close_failures:
            message += "; " + "; ".join(close_failures)
        retained_name = (
            quarantine.name
            if quarantine is not None
            else (
                error.retained_name
                if isinstance(
                    error,
                    _SchedulerDoctorQuarantineTransitionFailure,
                )
                else None
            )
        )
        if retained_name is not None:
            raise _SchedulerDoctorDeleteQuarantineFailure(
                binding.namespace_path / retained_name,
                message,
            ) from error
        if close_failures:
            raise RuntimeError(message) from error
        raise
    close_failures = _close_scheduler_doctor_candidate_descriptors(
        (quarantine.descriptor,)
    )
    if close_failures:
        raise _SchedulerDoctorDeleteQuarantineFailure(
            None,
            "; ".join(close_failures),
        )


def _close_scheduler_doctor_cleanup_custody(
    custody: tuple[
        tuple[str, int, tuple[int, int, int] | None], ...
    ],
) -> tuple[list[str], tuple[_SchedulerDoctorAbandonedDescriptorCustody, ...]]:
    failures: list[str] = []
    abandoned: list[_SchedulerDoctorAbandonedDescriptorCustody] = []
    for role, descriptor, identity in custody:
        try:
            os.close(descriptor)
        except OSError as error:
            failures.append(f"{role} descriptor close failed: {error}")
            abandoned.append(
                _SchedulerDoctorAbandonedDescriptorCustody(
                    role=role,
                    descriptor=descriptor,
                    identity=identity,
                    state="close-uncertain",
                )
            )
    return failures, tuple(abandoned)


def _cleanup_scheduler_doctor_test_session() -> None:
    global _SCHEDULER_DOCTOR_TEST_SESSION
    global _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD
    global _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE

    binding = _SCHEDULER_DOCTOR_TEST_SESSION
    lease_descriptor = _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD
    if _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE is not None:
        failure = _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE
        raise RuntimeError(
            "scheduler-doctor active-session cleanup previously failed; "
            f"retained for inspection: {failure.retained_path}: "
            f"{failure.reason}"
        )
    if binding is None:
        if lease_descriptor is not None:
            try:
                lease_identity = _scheduler_doctor_test_object_identity(
                    os.fstat(lease_descriptor)
                )
            except OSError:
                lease_identity = None
            message = (
                "scheduler-doctor fixture lease existed without session custody"
            )
            _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
                _SchedulerDoctorSessionCleanupFailure(
                    retained_path=None,
                    reason=message,
                    abandoned_custody=(
                        _SchedulerDoctorAbandonedDescriptorCustody(
                            "module-lease",
                            lease_descriptor,
                            lease_identity,
                            "retained-open",
                        ),
                    ),
                )
            )
            raise RuntimeError(message)
        return
    if lease_descriptor is None:
        message = "scheduler-doctor fixture session custody is incomplete"
        _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
            _SchedulerDoctorSessionCleanupFailure(
                retained_path=binding.path,
                reason=message,
                abandoned_custody=(
                    _SchedulerDoctorAbandonedDescriptorCustody(
                        "liveness",
                        binding.liveness_descriptor,
                        binding.liveness_identity,
                        "retained-open",
                    ),
                    _SchedulerDoctorAbandonedDescriptorCustody(
                        "session",
                        binding.descriptor,
                        binding.identity,
                        "retained-open",
                    ),
                    _SchedulerDoctorAbandonedDescriptorCustody(
                        "namespace",
                        binding.namespace_descriptor,
                        binding.namespace_identity,
                        "retained-open",
                    ),
                ),
            )
        )
        raise RuntimeError(message)

    module_lease_identity: tuple[int, int, int] | None = None
    try:
        module_lease_identity = _scheduler_doctor_test_object_identity(
            os.fstat(lease_descriptor)
        )
    except OSError:
        pass
    retained_custody = (
        _SchedulerDoctorAbandonedDescriptorCustody(
            "liveness",
            binding.liveness_descriptor,
            binding.liveness_identity,
            "retained-open",
        ),
        _SchedulerDoctorAbandonedDescriptorCustody(
            "session",
            binding.descriptor,
            binding.identity,
            "retained-open",
        ),
        _SchedulerDoctorAbandonedDescriptorCustody(
            "namespace",
            binding.namespace_descriptor,
            binding.namespace_identity,
            "retained-open",
        ),
        _SchedulerDoctorAbandonedDescriptorCustody(
            "module-lease",
            lease_descriptor,
            module_lease_identity,
            "retained-open",
        ),
    )
    _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
        _SchedulerDoctorSessionCleanupFailure(
            retained_path=binding.path,
            reason="cleanup in progress",
            abandoned_custody=retained_custody,
        )
    )

    liveness_probe = -1
    try:
        _validate_scheduler_doctor_session_lease(
            binding.namespace_path / _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
            lease_descriptor,
            parent_fd=binding.namespace_descriptor,
        )
        _validate_scheduler_doctor_active_session_binding(binding)
        liveness_probe, _identity = _open_scheduler_doctor_liveness_descriptor(
            binding.path,
            binding.descriptor,
            expected_mount_identity=binding.mount_identity,
            expected_identity=binding.liveness_identity,
        )
    except BaseException as error:
        _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
            _SchedulerDoctorSessionCleanupFailure(
                retained_path=binding.path,
                reason=str(error),
                abandoned_custody=retained_custody,
            )
        )
        raise

    try:
        os.close(binding.liveness_descriptor)
    except OSError as error:
        probe_failures, abandoned_probe = (
            _close_scheduler_doctor_cleanup_custody(
                (
                    (
                        "liveness-probe",
                        liveness_probe,
                        binding.liveness_identity,
                    ),
                )
            )
        )
        message = f"liveness descriptor close failed: {error}"
        if probe_failures:
            message += "; " + "; ".join(probe_failures)
        uncertain_liveness = _SchedulerDoctorAbandonedDescriptorCustody(
            "liveness",
            binding.liveness_descriptor,
            binding.liveness_identity,
            "close-uncertain",
        )
        _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
            _SchedulerDoctorSessionCleanupFailure(
                retained_path=binding.path,
                reason=message,
                abandoned_custody=(
                    uncertain_liveness,
                    *retained_custody[1:],
                    *abandoned_probe,
                ),
            )
        )
        raise RuntimeError(message) from error

    probe_binding = _SchedulerDoctorActiveSessionBinding(
        path=binding.path,
        namespace_path=binding.namespace_path,
        namespace_descriptor=binding.namespace_descriptor,
        namespace_identity=binding.namespace_identity,
        namespace_mount_identity=binding.namespace_mount_identity,
        descriptor=binding.descriptor,
        identity=binding.identity,
        mount_identity=binding.mount_identity,
        liveness_descriptor=liveness_probe,
        liveness_identity=binding.liveness_identity,
    )
    _SCHEDULER_DOCTOR_TEST_SESSION = probe_binding
    retained_probe_custody = (
        _SchedulerDoctorAbandonedDescriptorCustody(
            "liveness-probe",
            liveness_probe,
            binding.liveness_identity,
            "retained-open",
        ),
        *retained_custody[1:],
    )
    _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
        _SchedulerDoctorSessionCleanupFailure(
            retained_path=binding.path,
            reason="cleanup in progress after liveness transfer",
            abandoned_custody=retained_probe_custody,
        )
    )
    primary: BaseException | None = None
    try:
        if _scheduler_doctor_liveness_is_busy(liveness_probe):
            raise RuntimeError(
                "scheduler-doctor active session is still held by a child"
            )
        _remove_bound_scheduler_doctor_active_session(binding, liveness_probe)
    except BaseException as error:
        primary = error

    if primary is not None:
        retained_path = (
            primary.retained_path
            if isinstance(primary, _SchedulerDoctorDeleteQuarantineFailure)
            else binding.path
        )
        _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
            _SchedulerDoctorSessionCleanupFailure(
                retained_path=retained_path,
                reason=str(primary),
                abandoned_custody=retained_probe_custody,
            )
        )
        raise RuntimeError(str(primary)) from primary

    close_failures, abandoned = _close_scheduler_doctor_cleanup_custody(
        (
            ("session", binding.descriptor, binding.identity),
            (
                "namespace",
                binding.namespace_descriptor,
                binding.namespace_identity,
            ),
            ("liveness-probe", liveness_probe, binding.liveness_identity),
            ("module-lease", lease_descriptor, module_lease_identity),
        )
    )
    if close_failures:
        message = "; ".join(close_failures)
        _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = (
            _SchedulerDoctorSessionCleanupFailure(
                retained_path=None,
                reason=message,
                abandoned_custody=abandoned,
            )
        )
        raise RuntimeError(message)
    _SCHEDULER_DOCTOR_TEST_SESSION = None
    _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD = None
    _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE = None


def setUpModule() -> None:
    _install_scheduler_doctor_test_popen_wrapper()


def tearDownModule() -> None:
    try:
        _cleanup_scheduler_doctor_test_session()
    finally:
        _restore_scheduler_doctor_test_popen_wrapper()


def _scheduler_doctor_test_temporary_directory() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(
        prefix="scheduler-doctor.",
        dir=_scheduler_doctor_test_session_directory(),
    )


def snapshot_tree(root: Path) -> tuple[tuple[str, str, int, bytes | str | None], ...]:
    entries: list[tuple[str, str, int, bytes | str | None]] = []

    def visit(path: Path) -> None:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            entries.append((relative, "symlink", mode, os.readlink(path)))
            return
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode, None))
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        if stat.S_ISREG(metadata.st_mode):
            entries.append((relative, "file", mode, path.read_bytes()))
            return
        entries.append((relative, "other", mode, None))

    visit(root)
    return tuple(entries)


class SchedulerDoctorFixtureTests(unittest.TestCase):
    def test_temporary_root_ignores_ambient_tmpdir_and_cleans_up(self) -> None:
        session_root = _scheduler_doctor_test_session_directory()
        system_tmp = Path(os.path.realpath("/tmp"))
        expected_anchor = os.environ.get(
            _SCHEDULER_DOCTOR_TEST_EXPECTED_ANCHOR_ENV
        )
        if expected_anchor:
            self.assertTrue(
                session_root.is_relative_to(Path(os.path.realpath(expected_anchor)))
            )
        else:
            self.assertTrue(
                any(
                    session_root.is_relative_to(
                        Path(
                            os.path.realpath(
                                _scheduler_doctor_candidate_path(candidate)
                            )
                        )
                    )
                    for candidate in _scheduler_doctor_test_namespace_candidates()
                )
            )
        with mock.patch.dict(os.environ, {"TMPDIR": "/tmp"}):
            temporary_directory = _scheduler_doctor_test_temporary_directory()
        root = Path(os.path.realpath(temporary_directory.name))

        try:
            self.assertEqual(root.parent, session_root)
            sticky_fallback = _scheduler_doctor_linux_sticky_fallback_path()
            if sys.platform.startswith("linux") and session_root.is_relative_to(
                sticky_fallback
            ):
                self.assertTrue(root.is_relative_to(sticky_fallback))
            else:
                self.assertFalse(root.is_relative_to(system_tmp))
            self.assertEqual(
                session_root.parent.name,
                _SCHEDULER_DOCTOR_TEST_NAMESPACE_NAME,
            )
            self.assertEqual(
                session_root.parent.parent.name,
                _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME,
            )
            _validate_trusted_scheduler_doctor_test_root(session_root)
            (root / "cleanup-probe").write_text("fixture\n", encoding="utf-8")
        finally:
            temporary_directory.cleanup()

        self.assertFalse(root.exists())

    def test_namespace_candidates_never_resolve_the_account_home(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_mirror_canonical_account_home_directory",
                side_effect=AssertionError("account-home resolver must not run"),
            ),
            mock.patch.dict(
                os.environ,
                {
                    _SCHEDULER_DOCTOR_TEST_ANCHOR_ENV: os.fspath(REPO_ROOT),
                },
                clear=True,
            ),
        ):
            candidates = _scheduler_doctor_test_namespace_candidates()

        resolved_repo = Path(os.path.realpath(REPO_ROOT))
        resolved_candidates = tuple(
            Path(os.path.realpath(_scheduler_doctor_candidate_path(candidate)))
            for candidate in candidates
        )
        self.assertEqual(resolved_candidates.count(resolved_repo), 1)

    def test_darwin_platform_anchor_parent_uses_fixed_getconf_result(self) -> None:
        parent = Path("/private/var/folders/fixture/T")
        binding = _SchedulerDoctorBoundNamespaceCandidate(
            parent,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        result = subprocess.CompletedProcess(
            args=["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            returncode=0,
            stdout=f"{parent}\n",
            stderr="",
        )
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(subprocess, "run", return_value=result) as run,
            mock.patch(
                f"{__name__}._scheduler_doctor_darwin_temp_parent",
                return_value=binding,
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(
            candidates,
            (binding,),
        )
        run.assert_called_once_with(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def test_darwin_platform_anchor_parent_ignores_failed_getconf(self) -> None:
        result = subprocess.CompletedProcess(
            args=["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            returncode=1,
            stdout="",
            stderr="unavailable\n",
        )
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(subprocess, "run", return_value=result),
            mock.patch(
                f"{__name__}._bounded_darwin_user_temp_directories",
                return_value=(),
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(candidates, ())

    def test_darwin_platform_anchor_parent_uses_safe_ambient_tmpdir(self) -> None:
        parent = Path("/private/var/folders/fixture/T")
        binding = _SchedulerDoctorBoundNamespaceCandidate(
            parent,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        result = subprocess.CompletedProcess(
            args=["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            returncode=1,
            stdout="",
            stderr="unavailable\n",
        )
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(subprocess, "run", return_value=result) as run,
            mock.patch(
                f"{__name__}._scheduler_doctor_darwin_temp_parent",
                return_value=binding,
            ),
            mock.patch.dict(
                os.environ,
                {"TMPDIR": os.fspath(parent)},
                clear=True,
            ),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(
            candidates,
            (binding,),
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["TMPDIR"],
            "/private/var/folders/fixture/T",
        )

    def test_darwin_platform_anchor_parent_rejects_shared_tmpdir(self) -> None:
        result = subprocess.CompletedProcess(
            args=["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            returncode=0,
            stdout="/tmp\n",
            stderr="",
        )
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(subprocess, "run", return_value=result),
            mock.patch(
                f"{__name__}._bounded_darwin_user_temp_directories",
                return_value=(),
            ),
            mock.patch.dict(os.environ, {"TMPDIR": "/tmp"}, clear=True),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(candidates, ())

    def test_darwin_platform_anchor_parent_uses_bounded_scan_fallback(self) -> None:
        result = subprocess.CompletedProcess(
            args=["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            returncode=1,
            stdout="",
            stderr="unavailable\n",
        )
        scanned = Path("/private/var/folders/fixture/T")
        binding = _SchedulerDoctorBoundNamespaceCandidate(
            scanned,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(subprocess, "run", return_value=result),
            mock.patch(
                f"{__name__}._bounded_darwin_user_temp_directories",
                return_value=(scanned,),
            ) as scan,
            mock.patch(
                f"{__name__}._scheduler_doctor_darwin_temp_parent",
                return_value=binding,
            ),
            mock.patch.dict(os.environ, {"TMPDIR": "/tmp"}, clear=True),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(candidates, (binding,))
        scan.assert_called_once_with()

    def test_darwin_platform_anchor_parent_skips_stale_ambient_tmpdir(
        self,
    ) -> None:
        stale = Path("/private/var/folders/fixture/stale/T")
        getconf_parent = Path("/private/var/folders/fixture/current/T")
        getconf_binding = _SchedulerDoctorBoundNamespaceCandidate(
            getconf_parent,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        result = subprocess.CompletedProcess(
            args=["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            returncode=0,
            stdout=f"{getconf_parent}\n",
            stderr="",
        )

        def bind_parent(
            candidate: Path,
        ) -> _SchedulerDoctorBoundNamespaceCandidate | None:
            return None if candidate == stale else getconf_binding

        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(subprocess, "run", return_value=result) as run,
            mock.patch(
                f"{__name__}._scheduler_doctor_darwin_temp_parent",
                side_effect=bind_parent,
            ) as bind,
            mock.patch.dict(
                os.environ,
                {"TMPDIR": os.fspath(stale)},
                clear=True,
            ),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(candidates, (getconf_binding,))
        self.assertNotIn("TMPDIR", run.call_args.kwargs["env"])
        self.assertEqual(
            bind.call_args_list,
            [mock.call(stale), mock.call(getconf_parent)],
        )

    def test_darwin_temp_parent_stable_missing_is_unavailable(self) -> None:
        candidate = Path("/safe/missing")
        with (
            mock.patch.object(Path, "lstat", side_effect=FileNotFoundError()),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
            ) as bind,
        ):
            binding = _scheduler_doctor_darwin_temp_parent(candidate)

        self.assertIsNone(binding)
        bind.assert_not_called()

    def test_darwin_temp_parent_unreadable_probe_fails_closed(self) -> None:
        candidate = Path("/safe/unreadable")
        with (
            mock.patch.object(
                Path,
                "lstat",
                side_effect=PermissionError("denied"),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
            ) as bind,
            self.assertRaisesRegex(
                RuntimeError,
                "cannot inspect Darwin scheduler-doctor temp parent component",
            ),
        ):
            _scheduler_doctor_darwin_temp_parent(candidate)

        bind.assert_not_called()

    def test_darwin_temp_parent_binding_drift_fails_closed(self) -> None:
        candidate = Path("/safe/runtime")
        safe_parent = os.stat_result(
            (stat.S_IFDIR | 0o755, 1, 1, 1, 0, 0, 0, 0, 0, 0)
        )
        safe_runtime = os.stat_result(
            (
                stat.S_IFDIR | 0o700,
                2,
                1,
                1,
                os.geteuid(),
                os.getegid(),
                0,
                0,
                0,
                0,
            )
        )
        with (
            mock.patch.object(
                Path,
                "lstat",
                side_effect=(safe_parent, safe_runtime),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
                side_effect=MODULE.SyncError(
                    "Darwin temp parent changed while binding"
                ),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "Darwin temp parent changed while binding",
            ),
        ):
            _scheduler_doctor_darwin_temp_parent(candidate)

    def test_linux_platform_anchor_parent_uses_fixed_runtime_root(self) -> None:
        runtime_root = Path("/run/user") / str(os.geteuid())
        binding = _SchedulerDoctorBoundNamespaceCandidate(
            runtime_root,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch(
                f"{__name__}._scheduler_doctor_linux_runtime_parent_binding",
                return_value=binding,
            ) as probe,
            mock.patch(
                f"{__name__}._scheduler_doctor_linux_sticky_fallback_binding",
            ) as sticky_fallback,
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(
            candidates,
            (
                binding,
                _SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE,
            ),
        )
        probe.assert_called_once_with(runtime_root)
        sticky_fallback.assert_not_called()

    def test_linux_platform_anchor_parent_uses_sticky_fallback(self) -> None:
        runtime_root = Path("/run/user") / str(os.geteuid())
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch(
                f"{__name__}._scheduler_doctor_linux_runtime_parent_binding",
                return_value=None,
            ) as runtime_probe,
            mock.patch(
                f"{__name__}._scheduler_doctor_linux_sticky_fallback_binding",
            ) as sticky_fallback,
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(
            candidates,
            (_SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE,),
        )
        runtime_probe.assert_called_once_with(runtime_root)
        sticky_fallback.assert_not_called()

    def test_linux_namespace_prefers_safe_repo_before_sticky_fallback(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            repo_root = Path(directory)
            with (
                mock.patch.object(sys, "platform", "linux"),
                mock.patch(
                    f"{__name__}.REPO_ROOT",
                    repo_root,
                ),
                mock.patch(
                    f"{__name__}._scheduler_doctor_test_platform_anchor_parents",
                    return_value=(
                        _SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE,
                    ),
                ),
                mock.patch(
                    f"{__name__}._scheduler_doctor_linux_sticky_fallback_binding",
                    side_effect=AssertionError(
                        "sticky fallback must not precede a safe repo root"
                    ),
                ) as sticky_fallback,
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                candidates = _scheduler_doctor_test_namespace_candidates()
                namespace = _select_scheduler_doctor_test_namespace(candidates)

            self.assertEqual(
                candidates,
                (
                    Path(os.path.realpath(repo_root)),
                    _SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE,
                ),
            )
            self.assertTrue(namespace.is_relative_to(repo_root))
            sticky_fallback.assert_not_called()

    def test_linux_platform_anchor_parent_skips_stale_xdg_runtime(self) -> None:
        runtime_root = Path("/run/user") / str(os.geteuid())
        stale_runtime = runtime_root / "missing"

        binding = _SchedulerDoctorBoundNamespaceCandidate(
            runtime_root,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )

        def probe_candidate(
            candidate: Path,
        ) -> _SchedulerDoctorBoundNamespaceCandidate | None:
            return binding if candidate == runtime_root else None

        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch(
                f"{__name__}._scheduler_doctor_linux_runtime_parent_binding",
                side_effect=probe_candidate,
            ) as probe,
            mock.patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": os.fspath(stale_runtime)},
                clear=True,
            ),
        ):
            candidates = _scheduler_doctor_test_platform_anchor_parents()

        self.assertEqual(
            candidates,
            (
                binding,
                _SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE,
            ),
        )
        self.assertEqual(
            probe.call_args_list,
            [mock.call(stale_runtime), mock.call(runtime_root)],
        )

    def test_linux_runtime_parent_initial_missing_is_unavailable(self) -> None:
        candidate = Path("/safe/runtime/missing")
        safe_directory = os.stat_result(
            (stat.S_IFDIR | 0o755, 1, 1, 1, 0, 0, 0, 0, 0, 0)
        )
        with (
            mock.patch.object(
                Path,
                "lstat",
                side_effect=(
                    safe_directory,
                    safe_directory,
                    FileNotFoundError("missing"),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
            ) as bind,
        ):
            self.assertIsNone(
                _scheduler_doctor_linux_runtime_parent_binding(candidate)
            )

        bind.assert_not_called()

    def test_linux_runtime_parent_unreadable_probe_fails_closed(self) -> None:
        candidate = Path("/safe")
        with (
            mock.patch.object(
                Path,
                "lstat",
                side_effect=PermissionError("denied"),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
            ) as bind,
            self.assertRaisesRegex(
                RuntimeError,
                "cannot inspect Linux scheduler-doctor runtime parent component",
            ),
        ):
            _scheduler_doctor_linux_runtime_parent_binding(candidate)

        bind.assert_not_called()

    def test_linux_runtime_parent_binding_drift_fails_closed(self) -> None:
        candidate = Path("/safe/runtime")
        safe_parent = os.stat_result(
            (stat.S_IFDIR | 0o755, 1, 1, 1, 0, 0, 0, 0, 0, 0)
        )
        safe_runtime = os.stat_result(
            (
                stat.S_IFDIR | 0o700,
                2,
                1,
                1,
                os.geteuid(),
                os.getegid(),
                0,
                0,
                0,
                0,
            )
        )
        with (
            mock.patch.object(
                Path,
                "lstat",
                side_effect=(safe_parent, safe_runtime),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
                side_effect=MODULE.SyncError("runtime parent changed while binding"),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "runtime parent changed while binding",
            ),
        ):
            _scheduler_doctor_linux_runtime_parent_binding(candidate)

    def test_linux_runtime_parent_stable_wrong_leaf_owner_is_unavailable(
        self,
    ) -> None:
        candidate = Path("/safe/runtime")
        root_owned_directory = os.stat_result(
            (stat.S_IFDIR | 0o755, 1, 1, 1, 0, 0, 0, 0, 0, 0)
        )
        with (
            mock.patch.object(os, "geteuid", return_value=1000),
            mock.patch.object(
                Path,
                "lstat",
                side_effect=(root_owned_directory, root_owned_directory),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
            ) as bind,
        ):
            self.assertIsNone(
                _scheduler_doctor_linux_runtime_parent_binding(candidate)
            )

        bind.assert_not_called()

    def test_linux_runtime_parent_symlink_leaf_is_unavailable(self) -> None:
        candidate = Path("/safe/runtime")
        safe_parent = os.stat_result(
            (stat.S_IFDIR | 0o755, 1, 1, 1, 0, 0, 0, 0, 0, 0)
        )
        symlink_leaf = os.stat_result(
            (
                stat.S_IFLNK | 0o700,
                2,
                1,
                1,
                os.geteuid(),
                os.getegid(),
                0,
                0,
                0,
                0,
            )
        )
        with (
            mock.patch.object(
                Path,
                "lstat",
                side_effect=(safe_parent, symlink_leaf),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
            ) as bind,
        ):
            self.assertIsNone(
                _scheduler_doctor_linux_runtime_parent_binding(candidate)
            )

        bind.assert_not_called()

    def test_linux_runtime_parent_success_closes_bound_descriptor(self) -> None:
        candidate = Path("/safe/runtime")
        safe_parent = os.stat_result(
            (stat.S_IFDIR | 0o755, 1, 1, 1, 0, 0, 0, 0, 0, 0)
        )
        safe_runtime = os.stat_result(
            (
                stat.S_IFDIR | 0o700,
                2,
                1,
                1,
                os.geteuid(),
                os.getegid(),
                0,
                0,
                0,
                0,
            )
        )
        with (
            mock.patch.object(
                Path,
                "lstat",
                side_effect=(safe_parent, safe_runtime),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
                return_value=(
                    42,
                    (1, 2, stat.S_IFDIR),
                    (0o700, os.geteuid(), 0),
                ),
            ),
            mock.patch.object(os, "close") as close,
        ):
            binding = _scheduler_doctor_linux_runtime_parent_binding(candidate)

        self.assertEqual(
            binding,
            _SchedulerDoctorBoundNamespaceCandidate(
                candidate,
                (1, 2, stat.S_IFDIR),
                (0o700, os.geteuid(), 0),
            )
        )

        close.assert_called_once_with(42)

    def test_linux_sticky_root_accepts_only_exact_tmp_sticky_ancestor(
        self,
    ) -> None:
        effective_uid = os.geteuid()
        root = Path("/tmp/nested/sticky-root")

        self.assertTrue(
            _scheduler_doctor_linux_sticky_component_policy_is_safe(
                Path("/tmp"),
                root,
                (0o1777, 0, 0),
                effective_uid,
            )
        )
        self.assertFalse(
            _scheduler_doctor_linux_sticky_component_policy_is_safe(
                Path("/var/tmp"),
                root,
                (0o1777, 0, 0),
                effective_uid,
            )
        )

    def test_linux_sticky_root_disappearance_after_observation_fails_closed(
        self,
    ) -> None:
        with mock.patch.object(os, "stat", side_effect=FileNotFoundError):
            self.assertIsNone(
                _scheduler_doctor_linux_sticky_root_binding(Path("/missing"))
            )

        def disappear_while_binding(*_args, **_kwargs):
            try:
                raise FileNotFoundError("replaced after observation")
            except FileNotFoundError as error:
                raise MODULE.SyncError("cannot bind synthetic root") from error

        observed = os.stat_result(
            (stat.S_IFDIR | 0o1777, 1, 1, 1, os.geteuid(), 0, 0, 0, 0, 0)
        )
        with (
            mock.patch.object(os, "stat", return_value=observed),
            mock.patch.object(
                MODULE,
                "_bind_mirror_audit_child_directory",
                side_effect=disappear_while_binding,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "cannot bind Linux scheduler-doctor sticky root component",
            ),
        ):
            _scheduler_doctor_linux_sticky_root_binding(Path("/synthetic"))

    def test_linux_sticky_fallback_rejects_unsafe_precreation(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            root = Path(directory)
            sticky_root = root / "shared-tmp"
            sticky_root.mkdir(mode=0o700)
            sticky_root.chmod(0o1777)
            target = root / "foreign-target"
            target.mkdir(mode=0o700)
            fallback = sticky_root / (
                _SCHEDULER_DOCTOR_TEST_LINUX_STICKY_FALLBACK_PREFIX
                + str(os.geteuid())
            )
            try:
                for kind in ("symlink", "file", "wrong-mode"):
                    with self.subTest(kind=kind):
                        if kind == "symlink":
                            fallback.symlink_to(target, target_is_directory=True)
                        elif kind == "file":
                            fallback.write_text("foreign\n", encoding="utf-8")
                        else:
                            fallback.mkdir(mode=0o700)
                            fallback.chmod(0o755)
                        with (
                            mock.patch.object(sys, "platform", "linux"),
                            mock.patch(
                                f"{__name__}."
                                "_SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT",
                                sticky_root,
                            ),
                            self.assertRaisesRegex(
                                RuntimeError,
                                "sticky fallback has an unsafe type, owner, "
                                "or mode",
                            ),
                        ):
                            _scheduler_doctor_linux_sticky_fallback_binding()

                        self.assertTrue(fallback.exists() or fallback.is_symlink())
                        if fallback.is_dir() and not fallback.is_symlink():
                            fallback.chmod(0o700)
                            fallback.rmdir()
                        else:
                            fallback.unlink()
                self.assertTrue(target.is_dir())
            finally:
                sticky_root.chmod(0o700)

    def test_linux_sticky_fallback_rejects_wrong_owner(self) -> None:
        wrong_owner = os.geteuid() + 1
        root_read_fd, root_write_fd = os.pipe()
        child_read_fd, child_write_fd = os.pipe()
        try:
            with (
                mock.patch(
                    f"{__name__}._scheduler_doctor_linux_sticky_root_binding",
                    return_value=(
                        root_read_fd,
                        (1, 2, stat.S_IFDIR),
                        (0o1777, 0, 0),
                    ),
                ),
                mock.patch.object(os, "mkdir", side_effect=FileExistsError),
                mock.patch.object(
                    MODULE,
                    "_bind_mirror_audit_child_directory",
                    return_value=(
                        child_read_fd,
                        (1, 3, stat.S_IFDIR),
                        (0o700, wrong_owner, os.getegid()),
                    ),
                ) as bind_child,
                self.assertRaisesRegex(
                    RuntimeError,
                    "sticky fallback has an unsafe type, owner, or mode",
                ),
            ):
                _scheduler_doctor_linux_sticky_fallback_binding()

            bind_child.assert_called_once()
            for descriptor in (root_read_fd, child_read_fd):
                with self.assertRaises(OSError) as raised:
                    os.fstat(descriptor)
                self.assertEqual(raised.exception.errno, errno.EBADF)
        finally:
            os.close(root_write_fd)
            os.close(child_write_fd)
            for descriptor in (root_read_fd, child_read_fd):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def test_linux_sticky_fallback_policy_drift_fails_closed(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            sticky_root = Path(directory) / "shared-tmp"
            sticky_root.mkdir(mode=0o700)
            sticky_root.chmod(0o1777)
            try:
                with (
                    mock.patch.object(sys, "platform", "linux"),
                    mock.patch(
                        f"{__name__}._SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT",
                        sticky_root,
                    ),
                ):
                    binding = _scheduler_doctor_linux_sticky_fallback_binding()
                    assert binding is not None
                    binding.path.chmod(0o750)
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "sticky fallback changed after binding",
                    ):
                        _scheduler_doctor_rebind_sticky_candidate(binding)
                    binding.path.chmod(0o700)
            finally:
                sticky_root.chmod(0o700)

    def test_linux_sticky_fallback_replacement_fails_closed(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            sticky_root = Path(directory) / "shared-tmp"
            sticky_root.mkdir(mode=0o700)
            sticky_root.chmod(0o1777)
            try:
                with (
                    mock.patch.object(sys, "platform", "linux"),
                    mock.patch(
                        f"{__name__}._SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT",
                        sticky_root,
                    ),
                ):
                    binding = _scheduler_doctor_linux_sticky_fallback_binding()
                    assert binding is not None
                    replaced = sticky_root / "replaced-fallback"
                    binding.path.rename(replaced)
                    binding.path.mkdir(mode=0o700)
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "sticky fallback changed after binding",
                    ):
                        _scheduler_doctor_rebind_sticky_candidate(binding)
            finally:
                sticky_root.chmod(0o700)

    def test_linux_runtime_allocation_failure_uses_sticky_fallback(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            sticky_root = root / "shared-tmp"
            sticky_root.mkdir(mode=0o700)
            sticky_root.chmod(0o1777)
            runtime_container = runtime / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME
            real_mkdir = Path.mkdir

            def fail_runtime_allocation(
                path: Path,
                mode: int = 0o777,
                parents: bool = False,
                exist_ok: bool = False,
            ) -> None:
                if path == runtime_container:
                    raise OSError(errno.EROFS, "read-only runtime fixture")
                real_mkdir(
                    path,
                    mode=mode,
                    parents=parents,
                    exist_ok=exist_ok,
                )

            try:
                with (
                    mock.patch.object(sys, "platform", "linux"),
                    mock.patch(
                        f"{__name__}."
                        "_SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT",
                        sticky_root,
                    ),
                    mock.patch.object(Path, "mkdir", new=fail_runtime_allocation),
                ):
                    namespace = _select_scheduler_doctor_test_namespace(
                        (
                            runtime,
                            _SCHEDULER_DOCTOR_LINUX_STICKY_FALLBACK_CANDIDATE,
                        )
                    )
                    expected_fallback = (
                        _scheduler_doctor_linux_sticky_fallback_path()
                    )

                self.assertTrue(namespace.is_relative_to(expected_fallback))
                self.assertFalse(runtime_container.exists())
            finally:
                sticky_root.chmod(0o700)

    def test_linux_explicit_sticky_child_uses_fallback_aware_binding(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            sticky_root = Path(directory) / "shared-tmp"
            sticky_root.mkdir(mode=0o700)
            sticky_root.chmod(0o1777)
            try:
                with (
                    mock.patch.object(sys, "platform", "linux"),
                    mock.patch(
                        f"{__name__}."
                        "_SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT",
                        sticky_root,
                    ),
                ):
                    binding = _scheduler_doctor_linux_sticky_fallback_binding()
                    assert binding is not None
                    explicit_child = binding.path / "explicit"
                    explicit_child.mkdir(mode=0o700)
                    with mock.patch.object(
                        MODULE,
                        "_bind_mirror_trusted_account_home",
                        side_effect=AssertionError(
                            "sticky descendants must use the fallback-aware binder"
                        ),
                    ) as account_home_binder:
                        namespace = _select_scheduler_doctor_test_namespace(
                            (explicit_child,)
                        )
                        _validate_trusted_scheduler_doctor_test_root(namespace)

                    self.assertTrue(namespace.is_relative_to(explicit_child))
                    account_home_binder.assert_not_called()
            finally:
                sticky_root.chmod(0o700)

    def test_bound_linux_runtime_parent_identity_drift_fails_closed(self) -> None:
        candidate = Path("/run/user/1000")
        fallback = Path("/safe/fallback")
        binding = _SchedulerDoctorBoundNamespaceCandidate(
            candidate,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        with (
            mock.patch(
                "os.path.realpath",
                side_effect=lambda path: os.fspath(path),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
                return_value=(
                    42,
                    (1, 3, stat.S_IFDIR),
                    binding.access_policy,
                ),
            ) as bind,
            mock.patch.object(os, "close") as close,
            self.assertRaisesRegex(
                RuntimeError,
                "platform parent changed after binding",
            ),
        ):
            _select_scheduler_doctor_test_namespace((binding, fallback))

        bind.assert_called_once_with(candidate)
        close.assert_called_once_with(42)

    def test_bound_linux_runtime_parent_policy_drift_fails_closed(self) -> None:
        candidate = Path("/run/user/1000")
        fallback = Path("/safe/fallback")
        binding = _SchedulerDoctorBoundNamespaceCandidate(
            candidate,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        with (
            mock.patch(
                "os.path.realpath",
                side_effect=lambda path: os.fspath(path),
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_trusted_account_home",
                side_effect=(
                    MODULE.SyncError(
                        "canonical account home must be owned by the current "
                        "uid and not group/world writable"
                    ),
                    AssertionError("fallback must not be attempted"),
                ),
            ) as bind,
            self.assertRaisesRegex(
                RuntimeError,
                "platform parent changed after binding",
            ),
        ):
            _select_scheduler_doctor_test_namespace((binding, fallback))

        bind.assert_called_once_with(candidate)

    def test_bound_linux_runtime_parent_symlink_drift_fails_closed(self) -> None:
        candidate = Path("/run/user/1000")
        escaped = Path("/safe/escaped")
        binding = _SchedulerDoctorBoundNamespaceCandidate(
            candidate,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        with (
            mock.patch(
                f"{__name__}._scheduler_doctor_test_platform_anchor_parents",
                return_value=(binding,),
            ),
            mock.patch(
                "os.path.realpath",
                side_effect=lambda path: os.fspath(escaped)
                if Path(path) == candidate
                else os.fspath(path),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "platform parent changed after binding",
            ),
        ):
            _scheduler_doctor_test_namespace_candidates()

    @unittest.skipUnless(sys.platform == "darwin", "Darwin temp layout required")
    def test_darwin_platform_anchor_scan_enforces_global_entry_limit(self) -> None:
        with mock.patch(
            f"{__name__}._SCHEDULER_DOCTOR_TEST_DARWIN_TEMP_SCAN_ENTRY_LIMIT",
            0,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Darwin user temp directory scan exceeded its entry limit",
            ):
                _bounded_darwin_user_temp_directories()

    def test_platform_parent_is_stable_and_requires_no_random_anchor(self) -> None:
        parent = Path("/private/var/folders/fixture/T")
        with (
            mock.patch(
                f"{__name__}._scheduler_doctor_test_platform_anchor_parents",
                return_value=(parent,),
            ),
            mock.patch(
                f"{__name__}._scheduler_doctor_platform_parent_in_scope",
                return_value=True,
            ),
            mock.patch.object(tempfile, "TemporaryDirectory") as temporary,
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            candidates = _scheduler_doctor_test_namespace_candidates()

        self.assertEqual(
            candidates,
            (parent, Path(os.path.realpath(REPO_ROOT))),
        )
        temporary.assert_not_called()

    def test_platform_anchor_rejects_resolved_scope_change(self) -> None:
        candidate = Path("/private/var/folders/fixture/T")
        escaped = Path("/Users/fixture")
        with (
            mock.patch(
                f"{__name__}._scheduler_doctor_test_platform_anchor_parents",
                return_value=(candidate,),
            ),
            mock.patch(
                "os.path.realpath",
                side_effect=lambda path: str(escaped)
                if Path(path) == candidate
                else str(path),
            ),
            mock.patch.object(tempfile, "TemporaryDirectory") as temporary,
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            candidates = _scheduler_doctor_test_namespace_candidates()

        self.assertEqual(candidates, (Path(os.path.realpath(REPO_ROOT)),))
        temporary.assert_not_called()

    def test_stale_session_is_swept_before_reuse(self) -> None:
        session_root = _scheduler_doctor_test_session_directory()
        namespace = session_root / "sweep-fixture"
        namespace.mkdir(mode=0o700)
        stale_path = namespace / "session.stale"
        stale_path.mkdir(mode=0o700)
        _create_unlocked_scheduler_doctor_liveness_marker(stale_path)
        (stale_path / "residue").write_text("stale\n", encoding="utf-8")

        _sweep_stale_scheduler_doctor_sessions(namespace)

        self.assertFalse(stale_path.exists())

    def test_stale_sweep_preserves_busy_session_and_removes_unlocked_sibling(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            busy = namespace / "session.busy"
            busy.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(busy)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            marker = busy / _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME
            descriptor = os.open(marker, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            try:
                _sweep_stale_scheduler_doctor_sessions(namespace)
                self.assertTrue(busy.is_dir())
                self.assertFalse(stale.exists())
            finally:
                os.close(descriptor)

            _sweep_stale_scheduler_doctor_sessions(namespace)
            self.assertFalse(busy.exists())

    def test_stale_sweep_recovers_only_bounded_staging_residue(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            empty = namespace / f"{_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX}empty"
            empty.mkdir(mode=0o700)
            marked = namespace / f"{_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX}marked"
            marked.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(marked)

            _sweep_stale_scheduler_doctor_sessions(namespace)
            self.assertFalse(empty.exists())
            self.assertFalse(marked.exists())

            clean = namespace / f"{_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX}clean"
            clean.mkdir(mode=0o700)
            unsafe = namespace / f"{_SCHEDULER_DOCTOR_TEST_STAGING_PREFIX}unsafe"
            unsafe.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(unsafe)
            (unsafe / "unexpected").write_text("retain\n", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError,
                "staging directory retained unexpected entries",
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)
            self.assertTrue(clean.is_dir())
            self.assertTrue((unsafe / "unexpected").is_file())

    def test_delete_quarantine_nonce_collision_retries_atomically(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            first_nonce = b"a" * _SCHEDULER_DOCTOR_TEST_DELETE_NONCE_BYTES
            second_nonce = b"b" * _SCHEDULER_DOCTOR_TEST_DELETE_NONCE_BYTES
            collision = namespace / (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + first_nonce.hex()
            )
            collision.mkdir(mode=0o700)
            namespace_fd, _identity, _policy = (
                _bind_scheduler_doctor_test_root(namespace)
            )
            quarantine: _SchedulerDoctorDeleteQuarantineBinding | None = None
            try:
                mount_identity = _scheduler_doctor_stale_directory_mount_identity(
                    namespace_fd
                )
                with mock.patch.object(
                    os,
                    "urandom",
                    side_effect=(first_nonce, second_nonce),
                ):
                    quarantine = _create_scheduler_doctor_delete_quarantine(
                        namespace_fd,
                        mount_identity,
                    )
                self.assertEqual(
                    quarantine.name,
                    _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + second_nonce.hex(),
                )
                self.assertEqual(
                    stat.S_IMODE((namespace / quarantine.name).lstat().st_mode),
                    0o700,
                )
            finally:
                if quarantine is not None:
                    os.close(quarantine.descriptor)
                    (namespace / quarantine.name).rmdir()
                os.close(namespace_fd)

    def test_post_mkdir_validation_replacement_is_not_removed(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            nonce = b"r" * _SCHEDULER_DOCTOR_TEST_DELETE_NONCE_BYTES
            quarantine_name = (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + nonce.hex()
            )
            quarantine = namespace / quarantine_name
            moved_quarantine = namespace / f"{quarantine_name}.moved"
            replacement_marker = quarantine / "replacement"
            namespace_fd, _identity, _policy = (
                _bind_scheduler_doctor_test_root(namespace)
            )
            original_validate = (
                _validate_scheduler_doctor_delete_quarantine_binding
            )
            replaced = False

            def replace_before_first_validation(
                passed_namespace_fd: int,
                mount_identity: tuple[int, int | None],
                binding: _SchedulerDoctorDeleteQuarantineBinding,
                expected_names: tuple[str, ...],
                *,
                deadline: float,
            ) -> None:
                nonlocal replaced
                if not replaced:
                    replaced = True
                    quarantine.rename(moved_quarantine)
                    quarantine.mkdir(mode=0o700)
                    replacement_marker.write_text(
                        "retain\n",
                        encoding="utf-8",
                    )
                    raise RuntimeError(
                        "injected post-mkdir validation failure"
                    )
                original_validate(
                    passed_namespace_fd,
                    mount_identity,
                    binding,
                    expected_names,
                    deadline=deadline,
                )

            try:
                mount_identity = _scheduler_doctor_stale_directory_mount_identity(
                    namespace_fd
                )
                with (
                    mock.patch.object(os, "urandom", return_value=nonce),
                    mock.patch(
                        f"{__name__}._validate_scheduler_doctor_delete_quarantine_binding",
                        side_effect=replace_before_first_validation,
                    ),
                    self.assertRaises(
                        _SchedulerDoctorQuarantineTransitionFailure
                    ) as raised,
                ):
                    _create_scheduler_doctor_delete_quarantine(
                        namespace_fd,
                        mount_identity,
                    )
                self.assertEqual(raised.exception.retained_name, quarantine_name)
                self.assertTrue(moved_quarantine.is_dir())
                self.assertEqual(
                    replacement_marker.read_text(encoding="utf-8"),
                    "retain\n",
                )
            finally:
                os.close(namespace_fd)

    def test_quarantine_rename_failure_preserves_source_and_cleans_placeholder(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            source = namespace / "session.source"
            source.mkdir(mode=0o700)
            namespace_fd, _identity, _policy = (
                _bind_scheduler_doctor_test_root(namespace)
            )
            mount_identity = _scheduler_doctor_stale_directory_mount_identity(
                namespace_fd
            )
            source_identity = _scheduler_doctor_test_object_identity(
                source.lstat()
            )
            source_fd = _open_scheduler_doctor_stale_directory(
                namespace_fd,
                source.name,
                source_identity,
                expected_mount_identity=mount_identity,
                require_owner_private_directory=True,
            )
            try:
                with (
                    mock.patch.object(
                        os,
                        "rename",
                        side_effect=OSError(errno.EXDEV, "injected cross-device"),
                    ) as rename,
                    self.assertRaises(OSError) as raised,
                ):
                    _quarantine_scheduler_doctor_session(
                        namespace_fd,
                        mount_identity,
                        source.name,
                        source_fd,
                        source_identity,
                        deadline=time.monotonic() + 5.0,
                    )
                self.assertEqual(raised.exception.errno, errno.EXDEV)
                self.assertEqual(rename.call_count, 1)
                self.assertTrue(source.is_dir())
                self.assertFalse(
                    any(
                        child.name.startswith(
                            _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX
                        )
                        for child in namespace.iterdir()
                    )
                )
            finally:
                os.close(source_fd)
                os.close(namespace_fd)

    def test_post_rename_validation_failure_reports_retained_quarantine(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            source = namespace / "session.source"
            source.mkdir(mode=0o700)
            namespace_fd, _identity, _policy = (
                _bind_scheduler_doctor_test_root(namespace)
            )
            mount_identity = _scheduler_doctor_stale_directory_mount_identity(
                namespace_fd
            )
            source_identity = _scheduler_doctor_test_object_identity(
                source.lstat()
            )
            source_fd = _open_scheduler_doctor_stale_directory(
                namespace_fd,
                source.name,
                source_identity,
                expected_mount_identity=mount_identity,
                require_owner_private_directory=True,
            )
            original_stat = os.stat
            source_stat_calls = 0

            def fail_post_rename_source_stat(
                path: os.PathLike[str] | str | int,
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                nonlocal source_stat_calls
                if (
                    path == source.name
                    and kwargs.get("dir_fd") == namespace_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    source_stat_calls += 1
                    if source_stat_calls == 2:
                        raise PermissionError(
                            errno.EACCES,
                            "injected post-rename source revalidation failure",
                        )
                return original_stat(path, *args, **kwargs)

            retained: Path | None = None
            try:
                with (
                    mock.patch.object(
                        os,
                        "stat",
                        side_effect=fail_post_rename_source_stat,
                    ),
                    self.assertRaises(
                        _SchedulerDoctorQuarantineTransitionFailure
                    ) as raised,
                ):
                    _quarantine_scheduler_doctor_session(
                        namespace_fd,
                        mount_identity,
                        source.name,
                        source_fd,
                        source_identity,
                        deadline=time.monotonic() + 5.0,
                    )
                retained = namespace / raised.exception.retained_name
                self.assertFalse(source.exists())
                self.assertTrue(
                    (
                        retained
                        / _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME
                    ).is_dir()
                )
            finally:
                os.close(source_fd)
                os.close(namespace_fd)

            _sweep_stale_scheduler_doctor_sessions(namespace)
            assert retained is not None
            self.assertFalse(retained.exists())

    def test_stale_sweep_recovers_empty_delete_placeholder(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            quarantine = namespace / (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + "after-mkdir"
            )
            quarantine.mkdir(mode=0o700)

            _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(quarantine.exists())

    def test_stale_sweep_recovers_payload_after_quarantine_rename(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            quarantine = namespace / (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + "after-rename"
            )
            quarantine.mkdir(mode=0o700)
            payload = quarantine / _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME
            payload.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(payload)
            (payload / "residue").write_text("remove\n", encoding="utf-8")

            _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(quarantine.exists())

    def test_quarantine_delete_failure_is_recovered_by_next_sweep(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "residue").write_text("remove\n", encoding="utf-8")

            with (
                mock.patch(
                    f"{__name__}._apply_scheduler_doctor_stale_entry_plan",
                    side_effect=RuntimeError("injected quarantine delete failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected quarantine"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(stale.exists())
            quarantines = tuple(
                child
                for child in namespace.iterdir()
                if child.name.startswith(_SCHEDULER_DOCTOR_TEST_DELETE_PREFIX)
            )
            self.assertEqual(len(quarantines), 1)
            self.assertTrue(
                (
                    quarantines[0]
                    / _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME
                    / "residue"
                ).is_file()
            )

            _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(quarantines[0].exists())

    def test_quarantine_policy_drift_fails_before_payload_mutation(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            source = namespace / "session.source"
            source.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(source)
            namespace_fd, _identity, _policy = (
                _bind_scheduler_doctor_test_root(namespace)
            )
            mount_identity = _scheduler_doctor_stale_directory_mount_identity(
                namespace_fd
            )
            source_identity = _scheduler_doctor_test_object_identity(
                source.lstat()
            )
            source_fd = _open_scheduler_doctor_stale_directory(
                namespace_fd,
                source.name,
                source_identity,
                expected_mount_identity=mount_identity,
                require_owner_private_directory=True,
            )
            liveness_fd, liveness_identity = (
                _open_scheduler_doctor_liveness_descriptor(
                    source,
                    source_fd,
                    expected_mount_identity=mount_identity,
                )
            )
            self.assertFalse(_scheduler_doctor_liveness_is_busy(liveness_fd))
            quarantine: _SchedulerDoctorDeleteQuarantineBinding | None = None
            quarantine_path: Path | None = None
            try:
                quarantine = _quarantine_scheduler_doctor_session(
                    namespace_fd,
                    mount_identity,
                    source.name,
                    source_fd,
                    source_identity,
                    deadline=time.monotonic() + 5.0,
                )
                quarantine_path = namespace / quarantine.name
                quarantine_path.chmod(0o750)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "delete quarantine changed",
                ):
                    _delete_scheduler_doctor_quarantine_payload(
                        namespace_fd,
                        mount_identity,
                        _SchedulerDoctorDeleteQuarantineCandidate(
                            name=quarantine.name,
                            identity=quarantine.identity,
                            payload_identity=quarantine.payload_identity,
                            liveness_identity=liveness_identity,
                            liveness_present=True,
                            busy=False,
                            plans=(),
                        ),
                        deadline=time.monotonic() + 5.0,
                        quarantine_descriptor=quarantine.descriptor,
                        payload_descriptor=source_fd,
                        liveness_descriptor=liveness_fd,
                    )
                self.assertTrue(
                    (
                        quarantine_path
                        / _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME
                        / _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME
                    ).is_file()
                )
            finally:
                os.close(liveness_fd)
                os.close(source_fd)
                if quarantine is not None:
                    os.close(quarantine.descriptor)
                os.close(namespace_fd)

            assert quarantine_path is not None
            quarantine_path.chmod(0o700)
            _sweep_stale_scheduler_doctor_sessions(namespace)
            self.assertFalse(quarantine_path.exists())

    def test_delete_quarantine_inventory_limit_precedes_mutation(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            quarantine = namespace / (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + "bounded-payload"
            )
            quarantine.mkdir(mode=0o700)
            payload = quarantine / _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME
            payload.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(payload)
            first = payload / "first"
            second = payload / "second"
            first.write_text("first\n", encoding="utf-8")
            second.write_text("second\n", encoding="utf-8")

            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_ENTRY_LIMIT",
                    3,
                ),
                self.assertRaisesRegex(RuntimeError, "entry limit exceeded"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(first.read_text(encoding="utf-8"), "first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "second\n")

    def test_markerless_delete_payload_must_be_empty(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            quarantine = namespace / (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + "markerless-nonempty"
            )
            quarantine.mkdir(mode=0o700)
            payload = quarantine / _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME
            payload.mkdir(mode=0o700)
            residue = payload / "unproved"
            residue.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "markerless delete quarantine payload is not empty",
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(residue.read_text(encoding="utf-8"), "keep\n")

    def test_stale_sweep_recovers_empty_markerless_payload(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            quarantine = namespace / (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + "markerless-empty"
            )
            quarantine.mkdir(mode=0o700)
            payload = quarantine / _SCHEDULER_DOCTOR_TEST_DELETE_PAYLOAD_NAME
            payload.mkdir(mode=0o700)

            _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(quarantine.exists())

    def test_delete_quarantine_symlink_fails_before_sibling_mutation(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            root = Path(directory)
            namespace = root / "namespace"
            namespace.mkdir(mode=0o700)
            external = root / "external"
            external.mkdir(mode=0o700)
            quarantine = namespace / (
                _SCHEDULER_DOCTOR_TEST_DELETE_PREFIX + "symlink"
            )
            quarantine.symlink_to(external, target_is_directory=True)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)

            with self.assertRaisesRegex(
                RuntimeError,
                "delete quarantine is not an owner-private directory",
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertTrue(stale.is_dir())
            self.assertTrue(external.is_dir())

    def test_stale_sweep_missing_liveness_aborts_before_sibling_deletion(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            missing = namespace / "session.missing"
            missing.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)

            with self.assertRaises(FileNotFoundError):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertTrue(missing.is_dir())
            self.assertTrue(stale.is_dir())

    def test_stale_sweep_plans_all_sessions_before_any_deletion(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            clean = namespace / "session.a-clean"
            clean.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(clean)
            unsafe = namespace / "session.z-unsafe"
            unsafe.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(unsafe)
            (unsafe / "one" / "two").mkdir(parents=True, mode=0o700)

            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_DEPTH_LIMIT",
                    1,
                ),
                self.assertRaisesRegex(RuntimeError, "depth limit exceeded"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertTrue(clean.is_dir())
            self.assertTrue((unsafe / "one" / "two").is_dir())

    def test_stale_sweep_rejects_liveness_replacement_after_classification(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "marker").write_text("keep\n", encoding="utf-8")
            liveness_path = (
                stale / _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME
            )
            held_descriptor = -1
            inventory_count = 0
            real_inventory = _bounded_scheduler_doctor_stale_session_names

            def replace_after_classification(
                path: Path | int,
                *,
                deadline: float | None = None,
            ) -> tuple[str, ...]:
                nonlocal held_descriptor, inventory_count
                result = real_inventory(path, deadline=deadline)
                inventory_count += 1
                if inventory_count == 2:
                    held_descriptor = os.open(liveness_path, os.O_RDWR)
                    fcntl.flock(held_descriptor, fcntl.LOCK_SH)
                    liveness_path.unlink()
                    replacement = os.open(
                        liveness_path,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    os.close(replacement)
                return result

            try:
                with (
                    mock.patch(
                        f"{__name__}._bounded_scheduler_doctor_stale_session_names",
                        side_effect=replace_after_classification,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "liveness lease identity changed",
                    ),
                ):
                    _sweep_stale_scheduler_doctor_sessions(namespace)
                self.assertTrue((stale / "marker").is_file())
                self.assertTrue(liveness_path.is_file())
            finally:
                if held_descriptor >= 0:
                    os.close(held_descriptor)

    def test_real_child_liveness_holder_blocks_sweep_until_exit(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            busy = namespace / "session.busy"
            busy.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(busy)
            marker = busy / _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME
            descriptor = os.open(marker, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            metadata = os.fstat(descriptor)
            registry = _scheduler_doctor_liveness_registry_json(
                (
                    (
                        descriptor,
                        _scheduler_doctor_test_object_identity(metadata),
                    ),
                )
            )
            environment = os.environ.copy()
            environment[_SCHEDULER_DOCTOR_TEST_LIVENESS_REGISTRY_ENV] = registry
            with mock.patch.dict(
                os.environ,
                {_SCHEDULER_DOCTOR_TEST_LIVENESS_REGISTRY_ENV: registry},
            ):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import sys, time; print('ready', flush=True); time.sleep(30)",
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    text=True,
                )
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "ready")
                os.close(descriptor)
                descriptor = -1
                _sweep_stale_scheduler_doctor_sessions(namespace)
                self.assertTrue(busy.is_dir())
            finally:
                process.terminate()
                process.wait(timeout=10)
                if process.stdout is not None:
                    process.stdout.close()
                if descriptor >= 0:
                    os.close(descriptor)

            _sweep_stale_scheduler_doctor_sessions(namespace)
            self.assertFalse(busy.exists())

    def test_stale_session_inventory_accepts_exact_entry_limit(self) -> None:
        class TrackedScandir:
            def __init__(self, names: list[str]) -> None:
                self._names = iter(names)
                self.read_count = 0
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback) -> None:
                self.close()

            def __iter__(self):
                return self

            def __next__(self) -> Path:
                name = next(self._names)
                self.read_count += 1
                return Path(name)

            def close(self) -> None:
                self.closed = True

        names = [
            f"session.{index:04d}"
            for index in reversed(
                range(_SCHEDULER_DOCTOR_TEST_NAMESPACE_ENTRY_LIMIT)
            )
        ]
        names.append(_SCHEDULER_DOCTOR_TEST_LOCK_NAME)
        iterator = TrackedScandir(names)
        namespace = Path("/bounded-scheduler-doctor-fixture")
        with mock.patch.object(os, "scandir", return_value=iterator):
            result = _bounded_scheduler_doctor_stale_session_names(namespace)

        self.assertTrue(iterator.closed)
        self.assertEqual(
            iterator.read_count,
            _SCHEDULER_DOCTOR_TEST_NAMESPACE_ENTRY_LIMIT + 1,
        )
        self.assertEqual(
            len(result),
            _SCHEDULER_DOCTOR_TEST_NAMESPACE_ENTRY_LIMIT,
        )
        self.assertEqual(
            result,
            tuple(sorted(names[:-1], key=os.fsencode)),
        )

    def test_stale_session_inventory_stops_at_limit_plus_one(self) -> None:
        class TrackedScandir:
            def __init__(self, names: list[str]) -> None:
                self._names = iter(names)
                self.read_count = 0
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback) -> None:
                self.close()

            def __iter__(self):
                return self

            def __next__(self) -> Path:
                name = next(self._names)
                self.read_count += 1
                return Path(name)

            def close(self) -> None:
                self.closed = True

        names = [_SCHEDULER_DOCTOR_TEST_LOCK_NAME] + [
            f"session.{index:04d}"
            for index in range(
                _SCHEDULER_DOCTOR_TEST_NAMESPACE_ENTRY_LIMIT + 32
            )
        ]
        iterator = TrackedScandir(names)
        namespace = Path("/bounded-scheduler-doctor-fixture")
        with (
            mock.patch.object(os, "scandir", return_value=iterator),
            self.assertRaisesRegex(
                RuntimeError,
                "too many scheduler-doctor fixture namespace entries",
            ),
        ):
            _bounded_scheduler_doctor_stale_session_names(namespace)

        self.assertTrue(iterator.closed)
        self.assertEqual(
            iterator.read_count,
            _SCHEDULER_DOCTOR_TEST_NAMESPACE_ENTRY_LIMIT + 2,
        )

    def test_stale_session_sweep_removes_nested_entries_without_following_symlinks(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            root = Path(directory)
            namespace = root / "namespace"
            namespace.mkdir(mode=0o700)
            external = root / "external"
            external.mkdir(mode=0o700)
            marker = external / "marker"
            marker.write_text("keep\n", encoding="utf-8")
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            nested = stale / "nested"
            nested.mkdir(mode=0o700)
            (nested / "file").write_text("remove\n", encoding="utf-8")
            (stale / "external-link").symlink_to(external, target_is_directory=True)

            _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(stale.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_stale_session_sweep_rejects_same_device_mount_boundary(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            mounted = stale / "mounted"
            mounted.mkdir(mode=0o700)
            (mounted / "marker").write_text("keep\n", encoding="utf-8")
            mounted_identity = _scheduler_doctor_test_object_identity(
                mounted.lstat()
            )
            before = snapshot_tree(namespace)

            def mount_identity(descriptor: int) -> tuple[int, int | None]:
                metadata = os.fstat(descriptor)
                mount_id = (
                    102
                    if _scheduler_doctor_test_object_identity(metadata)
                    == mounted_identity
                    else 101
                )
                return metadata.st_dev, mount_id

            with (
                mock.patch(
                    f"{__name__}._scheduler_doctor_stale_directory_mount_identity",
                    side_effect=mount_identity,
                ),
                self.assertRaisesRegex(RuntimeError, "crosses a mount boundary"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(snapshot_tree(namespace), before)

    def test_stale_session_mount_probe_failure_does_not_delete(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "marker").write_text("keep\n", encoding="utf-8")
            before = snapshot_tree(namespace)

            with (
                mock.patch.object(
                    MODULE,
                    "_directory_mount_identity",
                    side_effect=MODULE.SyncError("mount identity unavailable"),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "cannot verify scheduler-doctor stale-session mount identity",
                ),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(snapshot_tree(namespace), before)

    def test_stale_session_mount_drift_during_revalidation_does_not_delete(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "marker").write_text("keep\n", encoding="utf-8")
            stale_identity = _scheduler_doctor_test_object_identity(stale.lstat())
            before = snapshot_tree(namespace)
            revalidating = False
            real_plan = _plan_scheduler_doctor_session_contents

            def mount_identity(descriptor: int) -> tuple[int, int | None]:
                metadata = os.fstat(descriptor)
                mount_id = (
                    102
                    if revalidating
                    and _scheduler_doctor_test_object_identity(metadata)
                    == stale_identity
                    else 101
                )
                return metadata.st_dev, mount_id

            def plan_after_mount_drift(
                session_fd: int,
                budget: _SchedulerDoctorStaleCleanupBudget,
                *,
                root_mount_identity: tuple[int, int | None],
            ) -> tuple[_SchedulerDoctorStaleEntryPlan, ...]:
                nonlocal revalidating
                result = real_plan(
                    session_fd,
                    budget,
                    root_mount_identity=root_mount_identity,
                )
                revalidating = True
                return result

            with (
                mock.patch(
                    f"{__name__}._scheduler_doctor_stale_directory_mount_identity",
                    side_effect=mount_identity,
                ),
                mock.patch(
                    f"{__name__}._plan_scheduler_doctor_session_contents",
                    side_effect=plan_after_mount_drift,
                ),
                self.assertRaisesRegex(RuntimeError, "crosses a mount boundary"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(snapshot_tree(namespace), before)

    def test_stale_session_mount_drift_before_apply_does_not_delete(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "marker").write_text("keep\n", encoding="utf-8")
            stale_identity = _scheduler_doctor_test_object_identity(stale.lstat())
            before = snapshot_tree(namespace)
            applying = False
            real_plan = _plan_scheduler_doctor_session_contents

            def mount_identity(descriptor: int) -> tuple[int, int | None]:
                metadata = os.fstat(descriptor)
                mount_id = (
                    102
                    if applying
                    and _scheduler_doctor_test_object_identity(metadata)
                    == stale_identity
                    else 101
                )
                return metadata.st_dev, mount_id

            def plan_before_apply_mount_drift(
                session_fd: int,
                budget: _SchedulerDoctorStaleCleanupBudget,
                *,
                root_mount_identity: tuple[int, int | None],
            ) -> tuple[_SchedulerDoctorStaleEntryPlan, ...]:
                nonlocal applying
                result = real_plan(
                    session_fd,
                    budget,
                    root_mount_identity=root_mount_identity,
                )
                applying = True
                return result

            with (
                mock.patch(
                    f"{__name__}._scheduler_doctor_stale_directory_mount_identity",
                    side_effect=mount_identity,
                ),
                mock.patch(
                    f"{__name__}._plan_scheduler_doctor_session_contents",
                    side_effect=plan_before_apply_mount_drift,
                ),
                self.assertRaisesRegex(RuntimeError, "crosses a mount boundary"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(snapshot_tree(namespace), before)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_stale_session_sweep_removes_fifo_leaf(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            fifo_path = stale / "fifo"
            os.mkfifo(fifo_path, mode=0o600)

            _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(stale.exists())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets required")
    def test_stale_session_sweep_removes_unix_socket_leaf(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import socket; s = socket.socket(socket.AF_UNIX); "
                    "s.bind('socket'); s.close()",
                ],
                cwd=stale,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertFalse(stale.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_stale_session_root_must_remain_a_directory(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            os.mkfifo(stale, mode=0o600)

            with self.assertRaisesRegex(
                RuntimeError,
                "stale-session root is not an owner-private directory",
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertTrue(stat.S_ISFIFO(stale.lstat().st_mode))

    def test_stale_leaf_owner_drift_does_not_match_plan(self) -> None:
        original = os.stat_result(
            (
                stat.S_IFIFO | 0o600,
                11,
                22,
                1,
                os.geteuid(),
                os.getegid(),
                0,
                0,
                0,
                0,
            )
        )
        changed_owner = os.stat_result(
            (
                stat.S_IFIFO | 0o600,
                11,
                22,
                1,
                os.geteuid() + 1,
                os.getegid(),
                0,
                0,
                0,
                0,
            )
        )
        plan = _SchedulerDoctorStaleEntryPlan(
            "fifo",
            _scheduler_doctor_test_object_identity(original),
            None,
        )

        self.assertFalse(
            _scheduler_doctor_stale_plan_matches(changed_owner, plan)
        )

    def test_stale_session_planning_rejects_device_leaf(self) -> None:
        metadata = os.stat_result(
            (
                stat.S_IFCHR | 0o600,
                11,
                22,
                1,
                os.geteuid(),
                os.getegid(),
                0,
                0,
                0,
                0,
            )
        )
        budget = _SchedulerDoctorStaleCleanupBudget(
            deadline=time.monotonic() + 30.0,
            remaining_entries=1,
            depth_limit=2,
        )

        with (
            mock.patch.object(os, "stat", return_value=metadata),
            self.assertRaisesRegex(
                RuntimeError,
                "unsupported scheduler-doctor stale-session entry",
            ),
        ):
            _plan_scheduler_doctor_stale_entry(
                -1,
                "device",
                budget,
                depth=2,
                root_mount_identity=(metadata.st_dev, 101),
            )

    def test_stale_session_entry_budget_failure_does_not_delete(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "first").write_text("one\n", encoding="utf-8")
            (stale / "second").write_text("two\n", encoding="utf-8")
            before = snapshot_tree(namespace)

            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_ENTRY_LIMIT",
                    2,
                ),
                self.assertRaisesRegex(RuntimeError, "entry limit exceeded"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(snapshot_tree(namespace), before)

    def test_stale_session_depth_budget_failure_does_not_delete(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            nested = stale / "one" / "two"
            nested.parent.mkdir(mode=0o700)
            nested.mkdir(mode=0o700)
            (nested / "file").write_text("keep\n", encoding="utf-8")
            before = snapshot_tree(namespace)

            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_STALE_CLEANUP_DEPTH_LIMIT",
                    2,
                ),
                self.assertRaisesRegex(RuntimeError, "depth limit exceeded"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(snapshot_tree(namespace), before)

    def test_stale_session_planning_timeout_does_not_delete(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "file").write_text("keep\n", encoding="utf-8")
            before = snapshot_tree(namespace)

            with (
                mock.patch.object(time, "monotonic", side_effect=(0.0, 31.0)),
                self.assertRaisesRegex(RuntimeError, "planning timed out"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertEqual(snapshot_tree(namespace), before)

    def test_stale_session_replacement_before_apply_is_preserved(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "original").write_text("keep\n", encoding="utf-8")
            displaced = namespace / "displaced"
            real_inventory = _bounded_scheduler_doctor_stale_session_names
            inventory_count = 0

            def replace_before_apply(
                path: Path | int,
                *,
                deadline: float | None = None,
            ) -> tuple[str, ...]:
                nonlocal inventory_count
                result = real_inventory(path, deadline=deadline)
                inventory_count += 1
                if inventory_count == 2:
                    stale.rename(displaced)
                    stale.mkdir(mode=0o700)
                    (stale / "replacement").write_text(
                        "keep\n",
                        encoding="utf-8",
                    )
                return result

            with (
                mock.patch(
                    f"{__name__}._bounded_scheduler_doctor_stale_session_names",
                    side_effect=replace_before_apply,
                ),
                self.assertRaisesRegex(RuntimeError, "changed"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertTrue((displaced / "original").is_file())
            self.assertTrue((stale / "replacement").is_file())

    def test_stale_session_access_policy_drift_before_apply_is_preserved(
        self,
    ) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            stale = namespace / "session.stale"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "original").write_text("keep\n", encoding="utf-8")
            real_inventory = _bounded_scheduler_doctor_stale_session_names
            inventory_count = 0

            def change_policy_before_apply(
                path: Path | int,
                *,
                deadline: float | None = None,
            ) -> tuple[str, ...]:
                nonlocal inventory_count
                result = real_inventory(path, deadline=deadline)
                inventory_count += 1
                if inventory_count == 2:
                    stale.chmod(0o755)
                return result

            with (
                mock.patch(
                    f"{__name__}._bounded_scheduler_doctor_stale_session_names",
                    side_effect=change_policy_before_apply,
                ),
                self.assertRaisesRegex(RuntimeError, "changed"),
            ):
                _sweep_stale_scheduler_doctor_sessions(namespace)

            self.assertTrue((stale / "original").is_file())
            self.assertEqual(stat.S_IMODE(stale.lstat().st_mode), 0o755)

    def test_shared_temp_checkout_falls_back_to_safe_anchor(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as fallback_directory:
            fallback = Path(fallback_directory)
            namespace = _select_scheduler_doctor_test_namespace(
                (Path("/tmp"), fallback)
            )

            self.assertTrue(namespace.is_relative_to(fallback))
            _validate_trusted_scheduler_doctor_test_root(namespace)

    def test_all_unsafe_candidates_report_a_clear_error(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "cannot select an owner-private scheduler-doctor test namespace",
        ):
            _select_scheduler_doctor_test_namespace((Path("/tmp"),))

    def test_anchor_override_must_be_absolute_and_precedes_fallbacks(self) -> None:
        with mock.patch.dict(
            os.environ,
            {_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV: "relative-anchor"},
        ):
            with self.assertRaisesRegex(RuntimeError, "must be absolute"):
                _scheduler_doctor_test_namespace_candidates()

        with _scheduler_doctor_test_temporary_directory() as anchor_directory:
            anchor = Path(anchor_directory)
            with _scheduler_doctor_test_temporary_directory() as platform_directory:
                platform_parent = Path(platform_directory)
                with (
                    mock.patch.dict(
                        os.environ,
                        {_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV: os.fspath(anchor)},
                    ),
                    mock.patch(
                        f"{__name__}._scheduler_doctor_test_platform_anchor_parents",
                        return_value=(platform_parent,),
                    ),
                    mock.patch(
                        f"{__name__}._scheduler_doctor_platform_parent_in_scope",
                        return_value=True,
                    ),
                ):
                    candidates = _scheduler_doctor_test_namespace_candidates()
                    namespace = _select_scheduler_doctor_test_namespace()
            self.assertEqual(
                candidates,
                (
                    Path(os.path.realpath(anchor)),
                    Path(os.path.realpath(platform_parent)),
                    Path(os.path.realpath(REPO_ROOT)),
                ),
            )
            self.assertTrue(namespace.is_relative_to(anchor))

    def test_stable_namespace_is_reused_and_sweeps_stale_sessions(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as candidate_directory:
            candidate = Path(candidate_directory)
            first = _select_scheduler_doctor_test_namespace((candidate,))
            stale = first / "session.interrupted"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "residue").write_text("stale\n", encoding="utf-8")

            second = _select_scheduler_doctor_test_namespace((candidate,))
            lease_path = second / _SCHEDULER_DOCTOR_TEST_LOCK_NAME
            descriptor = os.open(lease_path, os.O_RDWR)
            try:
                _acquire_scheduler_doctor_test_session_lease(descriptor)
                _validate_scheduler_doctor_session_lease(lease_path, descriptor)
                _sweep_stale_scheduler_doctor_sessions(second)
            finally:
                os.close(descriptor)

            self.assertEqual(first, second)
            self.assertTrue((second / _SCHEDULER_DOCTOR_TEST_LOCK_NAME).is_file())
            self.assertFalse(stale.exists())

    def test_existing_mode_0755_container_is_accepted(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as candidate_directory:
            candidate = Path(candidate_directory)
            container = candidate / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME
            container.mkdir(mode=0o755)
            container.chmod(0o755)

            namespace = _select_scheduler_doctor_test_namespace((candidate,))

            self.assertEqual(namespace.parent, container)
            self.assertEqual(stat.S_IMODE(container.lstat().st_mode), 0o755)
            _validate_owner_private_directory(namespace)

    def test_existing_writable_container_fails_closed(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as candidate_directory:
            candidate = Path(candidate_directory)
            container = candidate / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME
            container.mkdir(mode=0o700)
            container.chmod(0o770)
            try:
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "not group/world writable",
                ):
                    _select_scheduler_doctor_test_namespace((candidate,))
            finally:
                container.chmod(0o700)

    def test_existing_container_symlink_fails_closed(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as candidate_directory:
            candidate = Path(candidate_directory)
            target = candidate / "container-target"
            target.mkdir(mode=0o700)
            (candidate / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME).symlink_to(
                target,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                MODULE.SyncError,
                "non-symlink director",
            ):
                _select_scheduler_doctor_test_namespace((candidate,))

    def test_stable_fallback_is_exact_and_rejects_secondary_or_drift(self) -> None:
        candidate = Path("/fixture")
        stable = MODULE.SyncError(
            "canonical account-home ancestors must be root/current-owned and "
            "not group/world writable: /fixture"
        )
        secondary = MODULE.SyncError(f"{stable}; secondary cleanup failed")
        drift = MODULE.SyncError(
            "canonical account-home ancestor changed while binding it: /fixture"
        )

        self.assertTrue(
            _scheduler_doctor_test_anchor_is_stably_unsuitable(stable, candidate)
        )
        self.assertFalse(
            _scheduler_doctor_test_anchor_is_stably_unsuitable(secondary, candidate)
        )
        self.assertFalse(
            _scheduler_doctor_test_anchor_is_stably_unsuitable(drift, candidate)
        )

    def test_partial_allocation_is_removed_before_candidate_fallback(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as first_directory:
            with _scheduler_doctor_test_temporary_directory() as second_directory:
                first = Path(first_directory)
                second = Path(second_directory)
                blocked = (
                    first
                    / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME
                    / _SCHEDULER_DOCTOR_TEST_NAMESPACE_NAME
                )
                original_mkdir = Path.mkdir

                def reject_namespace(path: Path, *args: object, **kwargs: object) -> None:
                    if path == blocked:
                        raise OSError(errno.EROFS, "read-only test namespace")
                    original_mkdir(path, *args, **kwargs)

                with mock.patch.object(Path, "mkdir", new=reject_namespace):
                    namespace = _select_scheduler_doctor_test_namespace(
                        (first, second)
                    )
                self.assertTrue(namespace.is_relative_to(second))
                self.assertFalse(
                    (first / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME).exists()
                )

    def test_lock_open_eperm_removes_partial_candidate_and_falls_back(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as first_directory:
            with _scheduler_doctor_test_temporary_directory() as second_directory:
                first = Path(first_directory)
                second = Path(second_directory)
                original_open = os.open
                rejected = False

                def reject_first_lock(
                    path: str | bytes,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal rejected
                    if path == _SCHEDULER_DOCTOR_TEST_LOCK_NAME and not rejected:
                        rejected = True
                        raise OSError(errno.EPERM, "restricted test lease")
                    return original_open(path, flags, mode, dir_fd=dir_fd)

                with mock.patch.object(os, "open", new=reject_first_lock):
                    namespace = _select_scheduler_doctor_test_namespace(
                        (first, second)
                    )
                self.assertTrue(rejected)
                self.assertTrue(namespace.is_relative_to(second))
                self.assertFalse(
                    (first / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME).exists()
                )

    def test_lock_probe_reopens_after_concurrent_creation(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory)
            namespace_fd = os.open(
                namespace,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            original_open = os.open
            injected = False
            reopened_descriptor = -1

            def create_concurrent_lock(
                path: str | bytes,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal injected
                nonlocal reopened_descriptor
                if (
                    path == _SCHEDULER_DOCTOR_TEST_LOCK_NAME
                    and flags & os.O_EXCL
                    and not injected
                ):
                    injected = True
                    descriptor = original_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )
                    os.close(descriptor)
                    raise OSError(errno.EEXIST, "concurrent fixture lease")
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if path == _SCHEDULER_DOCTOR_TEST_LOCK_NAME and injected:
                    reopened_descriptor = descriptor
                return descriptor

            try:
                with mock.patch.object(os, "open", new=create_concurrent_lock):
                    _probe_scheduler_doctor_test_namespace_lock(
                        namespace,
                        namespace_fd,
                    )
            finally:
                os.close(namespace_fd)

            self.assertTrue(injected)
            metadata = (namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME).lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            with self.assertRaises(OSError) as raised:
                os.fstat(reopened_descriptor)
            self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_lock_probe_rejects_replacement_after_concurrent_creation(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory)
            namespace_fd = os.open(
                namespace,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            original_open = os.open
            injected = False
            replaced = False
            concurrent_lock_descriptor = -1

            def replace_concurrent_lock(
                path: str | bytes,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal concurrent_lock_descriptor
                nonlocal injected
                nonlocal replaced
                if path == _SCHEDULER_DOCTOR_TEST_LOCK_NAME and flags & os.O_EXCL:
                    concurrent_lock_descriptor = original_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )
                    injected = True
                    raise OSError(errno.EEXIST, "concurrent fixture lease")
                if path == _SCHEDULER_DOCTOR_TEST_LOCK_NAME and injected:
                    lock_path = namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME
                    lock_path.unlink()
                    descriptor = original_open(
                        path,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    os.close(descriptor)
                    replaced = True
                return original_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with (
                    mock.patch.object(os, "open", new=replace_concurrent_lock),
                    self.assertRaisesRegex(RuntimeError, "changed while opening"),
                ):
                    _probe_scheduler_doctor_test_namespace_lock(
                        namespace,
                        namespace_fd,
                    )
            finally:
                try:
                    if concurrent_lock_descriptor >= 0:
                        os.close(concurrent_lock_descriptor)
                finally:
                    os.close(namespace_fd)

            self.assertTrue(injected)
            self.assertTrue(replaced)
            self.assertTrue(
                (namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME).is_file()
            )

    def test_linux_sticky_checkout_subprocess_fixture(self) -> None:
        configured_root = os.environ.get(
            _SCHEDULER_DOCTOR_TEST_EXPECTED_LINUX_STICKY_ROOT_ENV
        )
        if configured_root is None:
            self.skipTest("Linux sticky-root subprocess fixture only")
        sticky_root = Path(os.path.abspath(configured_root))
        expected_fallback = sticky_root / (
            _SCHEDULER_DOCTOR_TEST_LINUX_STICKY_FALLBACK_PREFIX
            + str(os.geteuid())
        )
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch(
                f"{__name__}._SCHEDULER_DOCTOR_TEST_LINUX_STICKY_TEMP_ROOT",
                sticky_root,
            ),
            mock.patch(
                f"{__name__}._scheduler_doctor_linux_runtime_parent_binding",
                return_value=None,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_canonical_account_home_directory",
                side_effect=AssertionError("account home must not be resolved"),
            ),
        ):
            namespace = _ensure_scheduler_doctor_test_namespace()
            self.assertTrue(namespace.is_relative_to(expected_fallback))
            self.assertFalse(namespace.is_relative_to(REPO_ROOT))
            stale = namespace / "session.interrupted"
            stale.mkdir(mode=0o700)
            _create_unlocked_scheduler_doctor_liveness_marker(stale)
            (stale / "residue").write_text("stale\n", encoding="utf-8")

            session = _scheduler_doctor_test_session_directory()

            self.assertTrue(session.is_relative_to(namespace))
            self.assertFalse(stale.exists())
            _cleanup_scheduler_doctor_test_session()
            self.assertFalse(session.exists())
            self.assertTrue(namespace.is_dir())
            lock_metadata = (
                namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME
            ).lstat()
            self.assertTrue(stat.S_ISREG(lock_metadata.st_mode))
            self.assertEqual(stat.S_IMODE(lock_metadata.st_mode), 0o600)

            for case_type, case_name in (
                (
                    SchedulerDoctorFixtureTests,
                    "test_existing_mode_0755_container_is_accepted",
                ),
                (
                    SchedulerDoctorFixtureTests,
                    "test_existing_writable_container_fails_closed",
                ),
                (
                    SchedulerDoctorFixtureTests,
                    "test_existing_container_symlink_fails_closed",
                ),
                (
                    SchedulerDoctorTests,
                    "test_fixture_private_control_parent_is_bindable",
                ),
                (
                    SchedulerDoctorTests,
                    "test_mirror_quarantine_terminal_revalidates_primary_absence_anchor",
                ),
                (
                    SchedulerDoctorTests,
                    "test_mirror_walkers_transfer_fd_before_effectful_close_error",
                ),
            ):
                with self.subTest(case_name=case_name):
                    production_case = case_type(case_name)
                    production_result = unittest.TestResult()
                    production_case.run(production_result)
                    self.assertTrue(
                        production_result.wasSuccessful(),
                        msg=(
                            f"errors={production_result.errors!r}; "
                            f"failures={production_result.failures!r}"
                        ),
                    )

    def test_linux_sticky_fixture_binder_is_limited_to_fixture_subtree(
        self,
    ) -> None:
        fallback = _scheduler_doctor_linux_sticky_fallback_path()
        fixture_root = fallback / "fixture-root"
        fixture_child = fixture_root / "synthetic-account-home"
        outside = fallback / "unrelated"
        broad_fallback_child = fallback / "broad-fixture-child"
        non_sticky_child = Path("/var/lib/fixture-root/synthetic-account-home")
        fixture_binding = (
            11,
            (1, 2, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        production_binding = (
            12,
            (1, 3, stat.S_IFDIR),
            (0o700, os.geteuid(), os.getegid()),
        )
        production_binder = mock.Mock(return_value=production_binding)

        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch(
                f"{__name__}._bind_scheduler_doctor_test_root",
                return_value=fixture_binding,
            ) as fixture_binder,
        ):
            self.assertEqual(
                _bind_scheduler_doctor_fixture_account_home(
                    fixture_child,
                    fixture_root=fixture_root,
                    production_binder=production_binder,
                ),
                fixture_binding,
            )
            self.assertEqual(
                _bind_scheduler_doctor_fixture_account_home(
                    outside,
                    fixture_root=fixture_root,
                    production_binder=production_binder,
                ),
                production_binding,
            )
            self.assertEqual(
                _bind_scheduler_doctor_fixture_account_home(
                    non_sticky_child,
                    fixture_root=non_sticky_child.parent,
                    production_binder=production_binder,
                ),
                production_binding,
            )
            self.assertEqual(
                _bind_scheduler_doctor_fixture_account_home(
                    broad_fallback_child,
                    fixture_root=fallback,
                    production_binder=production_binder,
                ),
                production_binding,
            )

        fixture_binder.assert_called_once_with(fixture_child)
        self.assertEqual(
            production_binder.call_args_list,
            [
                mock.call(outside),
                mock.call(non_sticky_child),
                mock.call(broad_fallback_child),
            ],
        )

    def test_linux_shared_checkout_uses_stable_sticky_fallback(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            root = Path(directory)
            sticky_root = root / "shared-tmp"
            sticky_root.mkdir(mode=0o700)
            sticky_root.chmod(0o1777)
            checkout = sticky_root / "checkout"
            checkout.mkdir(mode=0o700)
            (checkout / "scripts").mkdir()
            (checkout / "tests").mkdir()
            shutil.copy2(SCRIPT_PATH, checkout / "scripts" / SCRIPT_PATH.name)
            shutil.copy2(Path(__file__), checkout / "tests" / Path(__file__).name)
            environment = os.environ.copy()
            environment["TMPDIR"] = os.fspath(sticky_root)
            environment.pop("XDG_RUNTIME_DIR", None)
            environment.pop(_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV, None)
            environment.pop(_SCHEDULER_DOCTOR_TEST_EXPECTED_ANCHOR_ENV, None)
            environment[
                _SCHEDULER_DOCTOR_TEST_EXPECTED_LINUX_STICKY_ROOT_ENV
            ] = os.fspath(sticky_root)
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "unittest",
                        "tests.test_scheduler_doctor."
                        "SchedulerDoctorFixtureTests."
                        "test_linux_sticky_checkout_subprocess_fixture",
                    ],
                    cwd=checkout,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            finally:
                sticky_root.chmod(0o700)

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_real_tmp_checkout_uses_unique_explicit_safe_anchor(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as anchor_directory:
            with tempfile.TemporaryDirectory(
                prefix="scheduler-doctor-checkout.",
                dir="/tmp",
            ) as checkout_directory:
                checkout = Path(checkout_directory)
                (checkout / "scripts").mkdir()
                (checkout / "tests").mkdir()
                shutil.copy2(SCRIPT_PATH, checkout / "scripts" / SCRIPT_PATH.name)
                shutil.copy2(Path(__file__), checkout / "tests" / Path(__file__).name)
                environment = os.environ.copy()
                environment["TMPDIR"] = "/tmp"
                environment[_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV] = anchor_directory
                environment[_SCHEDULER_DOCTOR_TEST_EXPECTED_ANCHOR_ENV] = (
                    anchor_directory
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "unittest",
                        "tests.test_scheduler_doctor.SchedulerDoctorFixtureTests."
                        "test_temporary_root_ignores_ambient_tmpdir_and_cleans_up",
                    ],
                    cwd=checkout,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "Darwin provides the default owner-private user temp directory",
    )
    def test_real_tmp_checkout_uses_default_darwin_safe_anchor(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="scheduler-doctor-checkout.",
            dir="/tmp",
        ) as checkout_directory:
            checkout = Path(checkout_directory)
            (checkout / "scripts").mkdir()
            (checkout / "tests").mkdir()
            shutil.copy2(SCRIPT_PATH, checkout / "scripts" / SCRIPT_PATH.name)
            shutil.copy2(Path(__file__), checkout / "tests" / Path(__file__).name)
            environment = os.environ.copy()
            environment["TMPDIR"] = "/tmp"
            environment.pop(_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV, None)
            environment.pop(_SCHEDULER_DOCTOR_TEST_EXPECTED_ANCHOR_ENV, None)
            _cleanup_scheduler_doctor_test_session()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "unittest",
                        "tests.test_scheduler_doctor.SchedulerDoctorFixtureTests."
                        "test_temporary_root_ignores_ambient_tmpdir_and_cleans_up",
                    ],
                    cwd=checkout,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            finally:
                _scheduler_doctor_test_session_directory()

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_existing_namespace_symlink_fails_closed(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as fallback_directory:
            fallback = Path(fallback_directory)
            parent = fallback / _SCHEDULER_DOCTOR_TEST_CONTAINER_NAME
            parent.mkdir(mode=0o700)
            target = fallback / "target"
            target.mkdir(mode=0o700)
            (parent / "scheduler-doctor").symlink_to(
                target,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "test fixture path is not a real directory",
            ):
                _select_scheduler_doctor_test_namespace((fallback,))

    def test_popen_wrapper_merges_liveness_and_rejects_custody_fds(self) -> None:
        _scheduler_doctor_test_session_directory()
        binding = _SCHEDULER_DOCTOR_TEST_SESSION
        lease_descriptor = _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD
        assert binding is not None
        assert lease_descriptor is not None
        reader, writer = os.pipe()
        sentinel = object()
        try:
            with mock.patch(
                f"{__name__}._SCHEDULER_DOCTOR_TEST_ORIGINAL_POPEN",
                return_value=sentinel,
            ) as original_popen:
                result = _scheduler_doctor_test_popen(
                    ["fixture"],
                    pass_fds=(reader,),
                    env={"FIXTURE": "yes"},
                )

            self.assertIs(result, sentinel)
            kwargs = original_popen.call_args.kwargs
            self.assertTrue(kwargs["close_fds"])
            self.assertIn(reader, kwargs["pass_fds"])
            self.assertIn(binding.liveness_descriptor, kwargs["pass_fds"])
            self.assertNotIn(binding.descriptor, kwargs["pass_fds"])
            self.assertNotIn(binding.namespace_descriptor, kwargs["pass_fds"])
            self.assertNotIn(lease_descriptor, kwargs["pass_fds"])
            registry = json.loads(
                kwargs["env"][_SCHEDULER_DOCTOR_TEST_LIVENESS_REGISTRY_ENV]
            )
            self.assertEqual(
                {entry["fd"] for entry in registry},
                {binding.liveness_descriptor},
            )
            self.assertEqual(kwargs["env"]["FIXTURE"], "yes")

            for protected in (
                binding.descriptor,
                binding.namespace_descriptor,
                lease_descriptor,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "custody descriptors",
                ):
                    _scheduler_doctor_test_popen(
                        ["fixture"],
                        pass_fds=(protected,),
                    )
            with self.assertRaisesRegex(RuntimeError, "close_fds=True"):
                _scheduler_doctor_test_popen(
                    ["fixture"],
                    close_fds=False,
                )
        finally:
            os.close(reader)
            os.close(writer)

    def test_popen_wrapper_rejects_registry_and_aliased_custody(self) -> None:
        _scheduler_doctor_test_session_directory()
        binding = _SCHEDULER_DOCTOR_TEST_SESSION
        lease_descriptor = _SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD
        assert binding is not None
        assert lease_descriptor is not None
        aliases = {
            "module-lease": os.dup(lease_descriptor),
            "session": os.dup(binding.descriptor),
            "namespace": os.dup(binding.namespace_descriptor),
        }
        try:
            for poison_kind, poisoned_descriptor in (
                ("exact", lease_descriptor),
                ("alias", aliases["module-lease"]),
            ):
                with self.subTest(registry_poison=poison_kind):
                    poisoned_registry = (
                        _scheduler_doctor_liveness_registry_json(
                            (
                                (
                                    poisoned_descriptor,
                                    _scheduler_doctor_test_object_identity(
                                        os.fstat(poisoned_descriptor)
                                    ),
                                ),
                            )
                        )
                    )
                    with (
                        mock.patch.dict(
                            os.environ,
                            {
                                _SCHEDULER_DOCTOR_TEST_LIVENESS_REGISTRY_ENV: (
                                    poisoned_registry
                                )
                            },
                        ),
                        mock.patch(
                            f"{__name__}._SCHEDULER_DOCTOR_TEST_ORIGINAL_POPEN"
                        ) as original_popen,
                        self.assertRaisesRegex(
                            RuntimeError,
                            "custody (descriptors|objects)",
                        ),
                    ):
                        _scheduler_doctor_test_popen(["fixture"])
                    original_popen.assert_not_called()

            with mock.patch(
                f"{__name__}._SCHEDULER_DOCTOR_TEST_ORIGINAL_POPEN"
            ) as original_popen:
                for role, descriptor in aliases.items():
                    with (
                        self.subTest(role=role),
                        self.assertRaisesRegex(
                            RuntimeError,
                            "custody objects",
                        ),
                    ):
                        _scheduler_doctor_test_popen(
                            ["fixture"],
                            pass_fds=(descriptor,),
                        )
            original_popen.assert_not_called()
        finally:
            for descriptor in aliases.values():
                os.close(descriptor)

    def test_initialization_publishes_only_validated_session(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION",
                    None,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD",
                    None,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE",
                    None,
                ),
                mock.patch(
                    f"{__name__}._ensure_scheduler_doctor_test_namespace",
                    return_value=namespace,
                ),
                mock.patch(
                    f"{__name__}._open_scheduler_doctor_liveness_descriptor",
                    side_effect=RuntimeError("injected pre-marker failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "pre-marker failure"),
            ):
                _scheduler_doctor_test_session_directory()
            self.assertEqual(
                {child.name for child in namespace.iterdir()},
                {_SCHEDULER_DOCTOR_TEST_LOCK_NAME},
            )

            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION",
                    None,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD",
                    None,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE",
                    None,
                ),
                mock.patch(
                    f"{__name__}._ensure_scheduler_doctor_test_namespace",
                    return_value=namespace,
                ),
                mock.patch(
                    f"{__name__}._validate_scheduler_doctor_active_session_binding",
                    side_effect=(
                        None,
                        RuntimeError("injected published validation failure"),
                    ),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "published validation failure",
                ),
            ):
                _scheduler_doctor_test_session_directory()
            self.assertEqual(
                {child.name for child in namespace.iterdir()},
                {_SCHEDULER_DOCTOR_TEST_LOCK_NAME},
            )

    def test_initialization_rollback_resolves_rename_after_effect(self) -> None:
        with _scheduler_doctor_test_temporary_directory() as directory:
            namespace = Path(directory) / "namespace"
            namespace.mkdir(mode=0o700)
            original_rename = os.rename
            publication_failed = False

            def rename_then_fail(
                source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                destination: (
                    str | bytes | os.PathLike[str] | os.PathLike[bytes]
                ),
                *,
                src_dir_fd: int | None = None,
                dst_dir_fd: int | None = None,
            ) -> None:
                nonlocal publication_failed
                original_rename(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                if (
                    not publication_failed
                    and os.fsdecode(source).startswith(
                        _SCHEDULER_DOCTOR_TEST_STAGING_PREFIX
                    )
                    and os.fsdecode(destination).startswith(
                        _SCHEDULER_DOCTOR_TEST_SESSION_PREFIX
                    )
                ):
                    publication_failed = True
                    raise RuntimeError("injected publish rename after effect")

            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION",
                    None,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD",
                    None,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE",
                    None,
                ),
                mock.patch(
                    f"{__name__}._ensure_scheduler_doctor_test_namespace",
                    return_value=namespace,
                ),
                mock.patch.object(os, "rename", side_effect=rename_then_fail),
                self.assertRaisesRegex(
                    RuntimeError,
                    "injected publish rename after effect",
                ),
            ):
                _scheduler_doctor_test_session_directory()
            self.assertTrue(publication_failed)
            self.assertIsNone(_SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE)
            self.assertEqual(
                {child.name for child in namespace.iterdir()},
                {_SCHEDULER_DOCTOR_TEST_LOCK_NAME},
            )

    def test_initialization_rollback_close_uncertainty_installs_fence(
        self,
    ) -> None:
        original_open = os.open
        original_close = os.close
        cases = (
            ("module-lease", False),
            ("module-lease", True),
            ("liveness", False),
        )
        for role, after_effect in cases:
            with self.subTest(role=role, after_effect=after_effect):
                with _scheduler_doctor_test_temporary_directory() as directory:
                    namespace = Path(directory) / "namespace"
                    namespace.mkdir(mode=0o700)
                    opened: dict[str, int] = {}
                    close_attempts: list[int] = []
                    injected = False

                    def observe_open(
                        path: str | bytes | os.PathLike[str] | int,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        descriptor = original_open(
                            path,
                            flags,
                            mode,
                            dir_fd=dir_fd,
                        )
                        if not isinstance(path, int):
                            name = Path(os.fsdecode(path)).name
                            opened_role: str | None = None
                            if name == _SCHEDULER_DOCTOR_TEST_LOCK_NAME:
                                opened_role = "module-lease"
                            elif (
                                name
                                == _SCHEDULER_DOCTOR_TEST_LIVENESS_LOCK_NAME
                            ):
                                opened_role = "liveness"
                            if opened_role is not None:
                                opened[opened_role] = descriptor
                                if opened_role == role:
                                    close_attempts.clear()
                        return descriptor

                    def fail_selected_close(descriptor: int) -> None:
                        nonlocal injected
                        close_attempts.append(descriptor)
                        if descriptor == opened.get(role) and not injected:
                            injected = True
                            if after_effect:
                                original_close(descriptor)
                            raise OSError(
                                errno.EIO,
                                f"injected {role} close failure",
                            )
                        original_close(descriptor)

                    try:
                        with (
                            mock.patch(
                                f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION",
                                None,
                            ),
                            mock.patch(
                                f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD",
                                None,
                            ),
                            mock.patch(
                                f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE",
                                None,
                            ),
                            mock.patch(
                                f"{__name__}._ensure_scheduler_doctor_test_namespace",
                                return_value=namespace,
                            ) as ensure_namespace,
                            mock.patch(
                                f"{__name__}._validate_scheduler_doctor_active_session_binding",
                                side_effect=RuntimeError(
                                    "injected initialization failure"
                                ),
                            ),
                            mock.patch.object(
                                os,
                                "open",
                                side_effect=observe_open,
                            ),
                            mock.patch.object(
                                os,
                                "close",
                                side_effect=fail_selected_close,
                            ),
                        ):
                            with self.assertRaisesRegex(
                                RuntimeError,
                                f"{role} descriptor close failed",
                            ):
                                _scheduler_doctor_test_session_directory()
                            self.assertTrue(injected)
                            selected = opened[role]
                            self.assertEqual(close_attempts.count(selected), 1)
                            failure = (
                                _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE
                            )
                            assert failure is not None
                            self.assertEqual(
                                [
                                    custody.role
                                    for custody in failure.abandoned_custody
                                ],
                                [role],
                            )
                            self.assertEqual(
                                failure.abandoned_custody[0].state,
                                "close-uncertain",
                            )
                            ensure_namespace.reset_mock()
                            with self.assertRaisesRegex(
                                RuntimeError,
                                "cleanup previously failed",
                            ):
                                _scheduler_doctor_test_session_directory()
                            ensure_namespace.assert_not_called()
                            with (
                                mock.patch(
                                    f"{__name__}._validate_owner_private_directory"
                                ) as validate_namespace,
                                self.assertRaisesRegex(
                                    RuntimeError,
                                    "stale sweep blocked",
                                ),
                            ):
                                _sweep_stale_scheduler_doctor_sessions(
                                    namespace
                                )
                            validate_namespace.assert_not_called()
                    finally:
                        selected = opened.get(role)
                        if not after_effect and selected is not None:
                            original_close(selected)
                        shutil.rmtree(namespace)

    def test_cleanup_close_uncertainty_installs_sticky_fence(self) -> None:
        original_close = os.close
        for after_effect in (False, True):
            with self.subTest(after_effect=after_effect):
                with _scheduler_doctor_test_temporary_directory() as directory:
                    namespace = Path(directory) / "namespace"
                    namespace.mkdir(mode=0o700)
                    namespace_metadata = namespace.lstat()
                    namespace_identity = _scheduler_doctor_test_object_identity(
                        namespace_metadata
                    )
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    flags |= getattr(os, "O_DIRECTORY", 0)
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    namespace_fd = os.open(namespace, flags)
                    mount_identity = (
                        _scheduler_doctor_stale_directory_mount_identity(
                            namespace_fd
                        )
                    )
                    lease_fd = os.open(
                        namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
                        os.O_RDWR | os.O_CREAT,
                        0o600,
                    )
                    fcntl.flock(lease_fd, fcntl.LOCK_EX)
                    session = namespace / "session.close-failure"
                    session.mkdir(mode=0o700)
                    session_identity = _scheduler_doctor_test_object_identity(
                        session.lstat()
                    )
                    session_fd = _open_scheduler_doctor_stale_directory(
                        namespace_fd,
                        session.name,
                        session_identity,
                        expected_mount_identity=mount_identity,
                        require_owner_private_directory=True,
                    )
                    liveness_fd, liveness_identity = (
                        _open_scheduler_doctor_liveness_descriptor(
                            session,
                            session_fd,
                            expected_mount_identity=mount_identity,
                            create=True,
                        )
                    )
                    fcntl.flock(liveness_fd, fcntl.LOCK_SH)
                    binding = _SchedulerDoctorActiveSessionBinding(
                        path=session,
                        namespace_path=namespace,
                        namespace_descriptor=namespace_fd,
                        namespace_identity=namespace_identity,
                        namespace_mount_identity=mount_identity,
                        descriptor=session_fd,
                        identity=session_identity,
                        mount_identity=mount_identity,
                        liveness_descriptor=liveness_fd,
                        liveness_identity=liveness_identity,
                    )
                    injected = False

                    def fail_liveness_close(descriptor: int) -> None:
                        nonlocal injected
                        if descriptor == liveness_fd and not injected:
                            injected = True
                            if after_effect:
                                original_close(descriptor)
                            raise OSError(errno.EIO, "injected close failure")
                        original_close(descriptor)

                    try:
                        with (
                            mock.patch(
                                f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION",
                                binding,
                            ),
                            mock.patch(
                                f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD",
                                lease_fd,
                            ),
                            mock.patch(
                                f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE",
                                None,
                            ),
                            mock.patch.object(os, "close", side_effect=fail_liveness_close),
                        ):
                            with self.assertRaisesRegex(
                                RuntimeError,
                                "descriptor close failed",
                            ):
                                _cleanup_scheduler_doctor_test_session()

                            self.assertTrue(injected)
                            self.assertIsNotNone(
                                _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE
                            )
                            with (
                                mock.patch(
                                    f"{__name__}._ensure_scheduler_doctor_test_namespace"
                                ) as ensure_namespace,
                                mock.patch(
                                    f"{__name__}._sweep_stale_scheduler_doctor_sessions"
                                ) as sweep,
                                self.assertRaisesRegex(
                                    RuntimeError,
                                    "cleanup previously failed",
                                ),
                            ):
                                _scheduler_doctor_test_session_directory()
                            ensure_namespace.assert_not_called()
                            sweep.assert_not_called()
                            with (
                                mock.patch(
                                    f"{__name__}._validate_owner_private_directory"
                                ) as validate_namespace,
                                self.assertRaisesRegex(
                                    RuntimeError,
                                    "stale sweep blocked",
                                ),
                            ):
                                _sweep_stale_scheduler_doctor_sessions(
                                    namespace
                                )
                            validate_namespace.assert_not_called()
                            self.assertTrue(session.is_dir())
                    finally:
                        if not after_effect:
                            original_close(liveness_fd)
                        original_close(session_fd)
                        original_close(namespace_fd)
                        original_close(lease_fd)
                        shutil.rmtree(namespace)

    def test_post_delete_close_uncertainty_keeps_fence_for_every_role(
        self,
    ) -> None:
        original_close = os.close
        real_remove = _remove_bound_scheduler_doctor_active_session
        for role in ("session", "namespace", "liveness-probe", "module-lease"):
            for after_effect in (False, True):
                with self.subTest(role=role, after_effect=after_effect):
                    with _scheduler_doctor_test_temporary_directory() as directory:
                        namespace = Path(directory) / "namespace"
                        namespace.mkdir(mode=0o700)
                        namespace_identity = (
                            _scheduler_doctor_test_object_identity(
                                namespace.lstat()
                            )
                        )
                        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                        flags |= getattr(os, "O_DIRECTORY", 0)
                        flags |= getattr(os, "O_NOFOLLOW", 0)
                        namespace_fd = os.open(namespace, flags)
                        mount_identity = (
                            _scheduler_doctor_stale_directory_mount_identity(
                                namespace_fd
                            )
                        )
                        lease_fd = os.open(
                            namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME,
                            os.O_RDWR | os.O_CREAT,
                            0o600,
                        )
                        fcntl.flock(lease_fd, fcntl.LOCK_EX)
                        session = namespace / "session.final-close"
                        session.mkdir(mode=0o700)
                        session_identity = (
                            _scheduler_doctor_test_object_identity(session.lstat())
                        )
                        session_fd = _open_scheduler_doctor_stale_directory(
                            namespace_fd,
                            session.name,
                            session_identity,
                            expected_mount_identity=mount_identity,
                            require_owner_private_directory=True,
                        )
                        liveness_fd, liveness_identity = (
                            _open_scheduler_doctor_liveness_descriptor(
                                session,
                                session_fd,
                                expected_mount_identity=mount_identity,
                                create=True,
                            )
                        )
                        fcntl.flock(liveness_fd, fcntl.LOCK_SH)
                        binding = _SchedulerDoctorActiveSessionBinding(
                            path=session,
                            namespace_path=namespace,
                            namespace_descriptor=namespace_fd,
                            namespace_identity=namespace_identity,
                            namespace_mount_identity=mount_identity,
                            descriptor=session_fd,
                            identity=session_identity,
                            mount_identity=mount_identity,
                            liveness_descriptor=liveness_fd,
                            liveness_identity=liveness_identity,
                        )
                        deletion_complete = False
                        injected_fd = -1

                        def observe_remove(
                            observed_binding: _SchedulerDoctorActiveSessionBinding,
                            probe: int,
                        ) -> None:
                            nonlocal deletion_complete
                            real_remove(observed_binding, probe)
                            deletion_complete = True

                        def fail_selected_close(descriptor: int) -> None:
                            nonlocal injected_fd
                            selected = {
                                "session": session_fd,
                                "namespace": namespace_fd,
                                "module-lease": lease_fd,
                            }.get(role)
                            if role == "liveness-probe":
                                selected = (
                                    descriptor
                                    if deletion_complete
                                    and descriptor
                                    not in {session_fd, namespace_fd, lease_fd}
                                    else -1
                                )
                            if (
                                deletion_complete
                                and injected_fd < 0
                                and descriptor == selected
                            ):
                                injected_fd = descriptor
                                if after_effect:
                                    original_close(descriptor)
                                raise OSError(
                                    errno.EIO,
                                    f"injected {role} close failure",
                                )
                            original_close(descriptor)

                        try:
                            with (
                                mock.patch(
                                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION",
                                    binding,
                                ),
                                mock.patch(
                                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD",
                                    lease_fd,
                                ),
                                mock.patch(
                                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE",
                                    None,
                                ),
                                mock.patch(
                                    f"{__name__}._remove_bound_scheduler_doctor_active_session",
                                    side_effect=observe_remove,
                                ),
                                mock.patch.object(
                                    os,
                                    "close",
                                    side_effect=fail_selected_close,
                                ),
                            ):
                                with self.assertRaisesRegex(
                                    RuntimeError,
                                    f"{role} descriptor close failed",
                                ):
                                    _cleanup_scheduler_doctor_test_session()
                                self.assertFalse(session.exists())
                                failure = (
                                    _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE
                                )
                                assert failure is not None
                                self.assertEqual(
                                    [
                                        item.role
                                        for item in failure.abandoned_custody
                                    ],
                                    [role],
                                )
                                self.assertEqual(
                                    failure.abandoned_custody[0].state,
                                    "close-uncertain",
                                )
                                with (
                                    mock.patch(
                                        f"{__name__}._ensure_scheduler_doctor_test_namespace"
                                    ) as ensure_namespace,
                                    mock.patch(
                                        f"{__name__}._sweep_stale_scheduler_doctor_sessions"
                                    ) as sweep,
                                    self.assertRaisesRegex(
                                        RuntimeError,
                                        "cleanup previously failed",
                                    ),
                                ):
                                    _scheduler_doctor_test_session_directory()
                                ensure_namespace.assert_not_called()
                                sweep.assert_not_called()
                        finally:
                            if not after_effect and injected_fd >= 0:
                                original_close(injected_fd)
                            shutil.rmtree(namespace)

    def test_namespace_lease_serializes_parallel_sweeps(self) -> None:
        _scheduler_doctor_test_session_directory()
        lease_path = (
            _ensure_scheduler_doctor_test_namespace()
            / _SCHEDULER_DOCTOR_TEST_LOCK_NAME
        )
        descriptor = os.open(lease_path, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def test_module_cleanup_keeps_stable_namespace_and_lock(self) -> None:
        session = _scheduler_doctor_test_session_directory()
        namespace = _ensure_scheduler_doctor_test_namespace()
        lease_path = namespace / _SCHEDULER_DOCTOR_TEST_LOCK_NAME

        try:
            _cleanup_scheduler_doctor_test_session()

            self.assertFalse(session.exists())
            self.assertTrue(namespace.is_dir())
            metadata = lease_path.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        finally:
            _scheduler_doctor_test_session_directory()

    def test_active_cleanup_identity_failures_fence_in_isolated_processes(
        self,
    ) -> None:
        script = (
            "import errno, json, sys\n"
            "from unittest import mock\n"
            "import tests.test_scheduler_doctor as t\n"
            "scenario = sys.argv[1]\n"
            "t.setUpModule()\n"
            "session = t._scheduler_doctor_test_session_directory()\n"
            "retained = session.with_name(session.name + '.original')\n"
            "probe = None\n"
            "if scenario == 'replaced':\n"
            "    session.rename(retained)\n"
            "    session.mkdir(mode=0o700)\n"
            "    probe = session / 'replacement-probe'\n"
            "    probe.write_text('replacement\\n', encoding='utf-8')\n"
            "elif scenario == 'missing':\n"
            "    session.rename(retained)\n"
            "elif scenario != 'unreadable':\n"
            "    raise RuntimeError('unknown scenario')\n"
            "original_stat = t.os.stat\n"
            "def deny_active_session_stat(path, *args, **kwargs):\n"
            "    if (path == session.name and "
            "kwargs.get('dir_fd') is not None and "
            "kwargs.get('follow_symlinks') is False):\n"
            "        raise PermissionError(errno.EACCES, 'fixture')\n"
            "    return original_stat(path, *args, **kwargs)\n"
            "try:\n"
            "    if scenario == 'unreadable':\n"
            "        with mock.patch.object(t.os, 'stat', "
            "side_effect=deny_active_session_stat):\n"
            "            t._cleanup_scheduler_doctor_test_session()\n"
            "    else:\n"
            "        t._cleanup_scheduler_doctor_test_session()\n"
            "except RuntimeError as error:\n"
            "    failure = t._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE\n"
            "    try:\n"
            "        t._scheduler_doctor_test_session_directory()\n"
            "    except RuntimeError as fence_error:\n"
            "        fence = str(fence_error)\n"
            "    else:\n"
            "        raise RuntimeError('cleanup failure did not fence reuse')\n"
            "    print(json.dumps({\n"
            "        'error': str(error),\n"
            "        'fence': fence,\n"
            "        'retained_path': (None if failure is None "
            "else str(failure.retained_path)),\n"
            "        'session': str(session),\n"
            "        'retained': str(retained),\n"
            "        'probe': (None if probe is None else str(probe)),\n"
            "    }, sort_keys=True), flush=True)\n"
            "else:\n"
            "    raise RuntimeError('cleanup unexpectedly succeeded')\n"
        )
        expectations = {
            "replaced": "active session object changed",
            "missing": "active session is missing",
            "unreadable": "active session is unreadable",
        }
        for scenario, expected in expectations.items():
            with (
                self.subTest(scenario=scenario),
                _scheduler_doctor_test_temporary_directory() as directory,
            ):
                environment = os.environ.copy()
                environment[_SCHEDULER_DOCTOR_TEST_ANCHOR_ENV] = directory
                environment[_SCHEDULER_DOCTOR_TEST_EXPECTED_ANCHOR_ENV] = (
                    directory
                )
                result = subprocess.run(
                    [sys.executable, "-c", script, scenario],
                    cwd=REPO_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
                )
                payload = json.loads(result.stdout)
                self.assertIn(expected, payload["error"])
                self.assertIn("cleanup previously failed", payload["fence"])
                session = Path(payload["session"])
                retained = Path(payload["retained"])
                if scenario == "replaced":
                    probe = Path(payload["probe"])
                    self.assertEqual(
                        probe.read_text(encoding="utf-8"),
                        "replacement\n",
                    )
                    self.assertTrue(retained.is_dir())
                elif scenario == "missing":
                    self.assertFalse(session.exists())
                    self.assertTrue(retained.is_dir())
                else:
                    self.assertTrue(session.is_dir())

    def test_cleanup_fences_orphaned_lease_descriptor(self) -> None:
        descriptor, writer = os.pipe()
        descriptor_open = True
        try:
            with (
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION",
                    None,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_LEASE_FD",
                    descriptor,
                ),
                mock.patch(
                    f"{__name__}._SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE",
                    None,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "without session custody",
                ):
                    _cleanup_scheduler_doctor_test_session()
                os.fstat(descriptor)
                failure = _SCHEDULER_DOCTOR_TEST_SESSION_CLEANUP_FAILURE
                self.assertIsInstance(
                    failure,
                    _SchedulerDoctorSessionCleanupFailure,
                )
                assert failure is not None
                self.assertIsNone(failure.retained_path)
                self.assertEqual(
                    failure.abandoned_custody,
                    (
                        _SchedulerDoctorAbandonedDescriptorCustody(
                            "module-lease",
                            descriptor,
                            _scheduler_doctor_test_object_identity(
                                os.fstat(descriptor)
                            ),
                            "retained-open",
                        ),
                    ),
                )
                os.close(descriptor)
                descriptor_open = False
                with self.assertRaises(OSError) as raised:
                    os.fstat(descriptor)
                self.assertEqual(raised.exception.errno, errno.EBADF)
        finally:
            if descriptor_open:
                os.close(descriptor)
            os.close(writer)

    def test_session_lease_retries_busy_lock_then_succeeds(self) -> None:
        busy = BlockingIOError(errno.EWOULDBLOCK, "fixture lease is busy")
        with (
            mock.patch.object(
                fcntl,
                "flock",
                side_effect=(busy, None),
            ) as flock,
            mock.patch.object(time, "monotonic", side_effect=(10.0, 10.0)),
            mock.patch.object(time, "sleep") as sleep,
        ):
            _acquire_scheduler_doctor_test_session_lease(
                123,
                timeout_seconds=1.0,
                retry_seconds=0.05,
            )

        self.assertEqual(flock.call_count, 2)
        flock.assert_called_with(123, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sleep.assert_called_once_with(0.05)

    def test_session_lease_timeout_is_bounded(self) -> None:
        busy = BlockingIOError(errno.EWOULDBLOCK, "fixture lease is busy")
        with (
            mock.patch.object(fcntl, "flock", side_effect=busy) as flock,
            mock.patch.object(time, "monotonic", side_effect=(10.0, 11.0)),
            mock.patch.object(time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "timed out acquiring"),
        ):
            _acquire_scheduler_doctor_test_session_lease(
                123,
                timeout_seconds=1.0,
                retry_seconds=0.05,
            )

        flock.assert_called_once_with(123, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sleep.assert_not_called()

    def test_session_lease_propagates_nonbusy_error(self) -> None:
        failure = OSError(errno.EBADF, "bad fixture descriptor")
        with (
            mock.patch.object(fcntl, "flock", side_effect=failure) as flock,
            mock.patch.object(time, "monotonic", return_value=10.0),
            mock.patch.object(time, "sleep") as sleep,
            self.assertRaises(OSError) as raised,
        ):
            _acquire_scheduler_doctor_test_session_lease(123)

        self.assertEqual(raised.exception.errno, errno.EBADF)
        flock.assert_called_once_with(123, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sleep.assert_not_called()


class SchedulerDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = _scheduler_doctor_test_temporary_directory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(os.path.realpath(self.tmpdir.name))
        self.user_home = self.root / "home"
        self.home = self.user_home / ".codex"
        self.home.mkdir(parents=True)
        self.path_home_patch = mock.patch.object(
            MODULE.Path,
            "home",
            return_value=self.user_home,
        )
        self.path_home_patch.start()
        self.addCleanup(self.path_home_patch.stop)
        production_account_home_binder = (
            MODULE._bind_mirror_trusted_account_home
        )
        self.production_account_home_binder = production_account_home_binder
        self.fixture_account_home_binder_patch = mock.patch.object(
            MODULE,
            "_bind_mirror_trusted_account_home",
            side_effect=lambda path: _bind_scheduler_doctor_fixture_account_home(
                path,
                fixture_root=self.root,
                production_binder=production_account_home_binder,
            ),
        )
        self.fixture_account_home_binder_patch.start()
        self.addCleanup(self.fixture_account_home_binder_patch.stop)
        self.host_mirror_private_control_parent = MODULE.MIRROR_PRIVATE_CONTROL_PARENT
        self.host_mirror_private_control_root_specs = (
            MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS
        )
        self.mirror_private_control_parent = (
            self.root / MODULE.MIRROR_PRIVATE_CONTROL_NAMESPACE_NAME
        )
        self.mirror_private_control_parent.mkdir(mode=0o700)
        self.mirror_private_control_parent_patch = mock.patch.object(
            MODULE,
            "MIRROR_PRIVATE_CONTROL_PARENT",
            self.mirror_private_control_parent,
        )
        self.mirror_private_control_parent_patch.start()
        self.addCleanup(self.mirror_private_control_parent_patch.stop)
        self.mirror_private_control_root_specs_patch = mock.patch.object(
            MODULE,
            "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
            (
                MODULE.MirrorPrivateControlRootSpec(
                    root_id="test-primary-home-v1",
                    parent_path=self.mirror_private_control_parent,
                    allocate=True,
                    account_home=self.root,
                    shared_parent=False,
                ),
            ),
        )
        self.mirror_private_control_root_specs_patch.start()
        self.addCleanup(self.mirror_private_control_root_specs_patch.stop)

    def test_fixture_private_control_parent_is_bindable(self) -> None:
        spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        binding = MODULE._bind_mirror_primary_control_parent(spec)
        self.assertIsNotNone(binding)
        assert binding is not None
        descriptor, identity, access_policy = binding
        try:
            metadata = self.mirror_private_control_parent.lstat()
            self.assertEqual(
                identity,
                MODULE._mirror_object_identity(metadata),
            )
            self.assertEqual(
                access_policy,
                MODULE._mirror_access_policy(metadata),
            )
        finally:
            os.close(descriptor)

    def write_runner(self) -> Path:
        runner = self.home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        return runner

    def write_legacy_scheduler_config(
        self,
        platform_name: str,
        runner: Path,
        *,
        include_mode: bool,
    ) -> tuple[MODULE.SchedulerPaths, Callable[[MODULE.SchedulerPaths], object], str]:
        paths = MODULE._scheduler_paths(platform_name, self.home)
        if platform_name == "macos":
            assert paths.launchd_plist is not None
            paths.launchd_plist.parent.mkdir(parents=True, exist_ok=True)
            command = "install"
            arguments = [str(runner), command]
            if include_mode:
                arguments.extend(("--mode", "public"))
            arguments.extend(
                ("--repo", "owner/public-sync", "--home", str(self.home))
            )
            payload = MODULE._launchd_plist(
                self.home,
                "owner/public-sync",
                19,
                runner,
            )
            payload["ProgramArguments"] = arguments
            paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
            return paths, MODULE._load_macos_scheduler_config, command
        assert platform_name == "linux"
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True, exist_ok=True)
        command = "install-private"
        arguments = [str(runner), command]
        if include_mode:
            arguments.extend(("--mode", "private"))
        arguments.extend(
            (
                "--repo",
                "owner/private-sync",
                "--base-repo",
                "owner/public-sync",
                "--owner",
                "private",
                "--home",
                str(self.home),
            )
        )
        legacy_exec = " ".join(
            MODULE._systemd_quote(argument) for argument in arguments
        )
        service_lines = MODULE._systemd_service(
            self.home,
            "owner/private-sync",
            runner,
            mode="private",
            base_repo="owner/public-sync",
            owner="private",
        ).splitlines()
        paths.systemd_service.write_text(
            "\n".join(
                f"ExecStart={legacy_exec}"
                if line.startswith("ExecStart=")
                else line
                for line in service_lines
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(19),
            encoding="utf-8",
        )
        return paths, MODULE._load_linux_scheduler_config, command

    def scheduler_status(
        self,
        platform_name: str,
        *,
        daemon_enabled: bool,
        strict: bool,
    ) -> tuple[int, dict[str, object], mock.Mock]:
        output = io.StringIO()
        arguments = [
            "status-scheduler",
            "--home",
            str(self.home),
            "--platform",
            platform_name,
            "--json",
        ]
        if strict:
            arguments.append("--strict")
        with (
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=None,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                return_value=daemon_enabled,
            ) as daemon_query,
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
            contextlib.redirect_stdout(output),
        ):
            status = MODULE.main(arguments)
        return status, json.loads(output.getvalue()), daemon_query

    def install_scheduler_quietly(
        self,
        repo: str,
        interval_minutes: int | None,
        platform_name: str,
        *,
        enable: bool = False,
        mode: str = "public",
        base_repo: str = MODULE.DEFAULT_PUBLIC_RELEASE_REPO,
        owner: str = "private",
    ) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.install_scheduler(
                self.home,
                repo,
                interval_minutes,
                platform_name,
                None,
                dry_run=False,
                enable=enable,
                mode=mode,
                base_repo=base_repo,
                owner=owner,
            )

    def write_pending_systemd_pair(
        self,
        user_home: Path,
        home: Path,
    ) -> MODULE.SchedulerPaths:
        runner = home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        with mock.patch.object(MODULE.Path, "home", return_value=user_home):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.install_scheduler(
                    home,
                    "owner/old",
                    17,
                    "linux",
                    None,
                    dry_run=False,
                    enable=False,
                )
            paths = MODULE._scheduler_paths("linux", home)
            assert paths.systemd_service is not None
            assert paths.systemd_timer is not None
            service_before = MODULE._scheduler_config_snapshot(paths.systemd_service)
            timer_before = MODULE._scheduler_config_snapshot(paths.systemd_timer)
            service_after = MODULE._systemd_service(
                home,
                "owner/new",
                runner,
            ).encode("utf-8")
            timer_after = MODULE._systemd_timer(29).encode("utf-8")
            marker = MODULE._scheduler_pair_transaction_path(paths)
            marker_before = MODULE._scheduler_config_snapshot(
                marker,
                MODULE.MAX_SCHEDULER_PAIR_TRANSACTION_BYTES,
            )
            MODULE._atomic_write_scheduler_config(
                marker,
                MODULE._scheduler_pair_transaction_payload(
                    service_before=service_before,
                    timer_before=timer_before,
                    service_after=service_after,
                    timer_after=timer_after,
                ),
                expected_snapshot=marker_before,
            )
            MODULE._atomic_write_scheduler_config(
                paths.systemd_service,
                service_after,
                expected_snapshot=service_before,
            )
            MODULE._atomic_write_scheduler_config(
                paths.systemd_timer,
                timer_after,
                expected_snapshot=timer_before,
            )
        return paths

    def write_skill(self, name: str, frontmatter_name: str) -> Path:
        skill_root = self.home / "skills" / name
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name}\n---\n",
            encoding="utf-8",
        )
        return skill_root

    def launchd_query_result(
        self,
        domain: str,
        state: str,
        *,
        label: str = MODULE.LAUNCHD_LABEL,
        uid: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        selected_uid = os.getuid() if uid is None else uid
        args = [
            "launchctl",
            "print",
            f"{domain}/{selected_uid}/{label}",
        ]
        if state == "enabled":
            return subprocess.CompletedProcess(args, 0, "service = enabled\n", "")
        if state == "disabled":
            if domain == MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN:
                evidence = (
                    "Bad request.\n"
                    f'Could not find service "{label}" in domain for uid: '
                    f"{selected_uid}"
                )
            elif domain == MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN:
                evidence = (
                    "Bad request.\n"
                    f'Could not find service "{label}" in domain for user gui: '
                    f"{selected_uid}"
                )
            else:
                raise AssertionError(f"unsupported launchd domain: {domain}")
            return subprocess.CompletedProcess(args, 113, "", evidence)
        if state == "denied":
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "Operation not permitted",
            )
        if state == "unrecognized":
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "Input/output error",
            )
        raise AssertionError(f"unsupported launchd state: {state}")

    def launchd_query_matrix(
        self,
        canonical_user_state: str,
        canonical_gui_state: str,
        *,
        legacy_overrides: dict[tuple[str, str], str] | None = None,
    ) -> list[subprocess.CompletedProcess[str]]:
        overrides = legacy_overrides or {}
        results = [
            self.launchd_query_result("user", canonical_user_state),
            self.launchd_query_result("gui", canonical_gui_state),
        ]
        for label in MODULE.LEGACY_LAUNCHD_LABELS:
            for domain in (
                MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
                MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
            ):
                results.append(
                    self.launchd_query_result(
                        domain,
                        overrides.get((label, domain), "disabled"),
                        label=label,
                    )
                )
        return results

    def test_scheduler_program_arguments_enforce_per_command_contracts(self) -> None:
        runner = str(self.home / "bin" / "runner")
        public = [
            runner,
            "install",
            "--repo",
            "owner/public-sync",
            "--home",
            str(self.home),
        ]
        private = [
            runner,
            "install-private",
            "--repo",
            "owner/private-sync",
            "--base-repo",
            "owner/public-sync",
            "--owner",
            "private",
            "--home",
            str(self.home),
        ]
        scheduled_public = [
            runner,
            "run-scheduled",
            "--mode",
            "public",
            *public[2:],
        ]
        scheduled_private = [
            runner,
            "run-scheduled",
            "--mode",
            "private",
            *private[2:],
        ]

        def parse(arguments: list[str]) -> MODULE.SchedulerConfig:
            return MODULE._parse_scheduler_program_arguments(
                arguments,
                platform_name="linux",
                config_paths=(self.home / "scheduler",),
                interval_minutes=17,
            )

        valid_cases = (
            (public, ("public", "owner/public-sync", "owner/public-sync", "public")),
            (
                private,
                ("private", "owner/private-sync", "owner/public-sync", "private"),
            ),
            (
                scheduled_public,
                ("public", "owner/public-sync", "owner/public-sync", "public"),
            ),
            (
                scheduled_private,
                ("private", "owner/private-sync", "owner/public-sync", "private"),
            ),
        )
        for arguments, expected in valid_cases:
            with self.subTest(valid=arguments[1]):
                config = parse(arguments)
                self.assertEqual(
                    (config.mode, config.repo, config.base_repo, config.owner),
                    expected,
                )

        invalid_flags = (
            (public, "--mode"),
            (public, "--base-repo"),
            (public, "--owner"),
            (private, "--mode"),
            (scheduled_public, "--unknown"),
        )
        for arguments, flag in invalid_flags:
            with (
                self.subTest(command=arguments[1], invalid=flag),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    re.escape(f"command {arguments[1]} does not allow {flag}"),
                ),
            ):
                parse([*arguments, flag, "value"])
            with (
                self.subTest(command=arguments[1], duplicate_invalid=flag),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    re.escape(f"scheduler config repeats {flag}"),
                ),
            ):
                parse([*arguments, flag, "value", flag, "value"])

        for arguments in (public, private, scheduled_private):
            for index in range(2, len(arguments), 2):
                flag = arguments[index]
                with (
                    self.subTest(command=arguments[1], duplicate=flag),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        re.escape(f"scheduler config repeats {flag}"),
                    ),
                ):
                    parse([*arguments, flag, arguments[index + 1]])
                with (
                    self.subTest(command=arguments[1], missing=flag),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        re.escape(f"scheduler config is missing {flag}"),
                    ),
                ):
                    parse([*arguments[:index], *arguments[index + 2 :]])
                option_like_value = [*arguments]
                option_like_value[index + 1] = "--unexpected"
                with (
                    self.subTest(command=arguments[1], option_like_value=flag),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        re.escape(
                            f"scheduler config value for {flag} "
                            "must not be an option token"
                        ),
                    ),
                ):
                    parse(option_like_value)

    def test_macos_scheduler_config_parses_private_run_scheduled(self) -> None:
        runner = self.home / "bin" / "runner with spaces"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        paths.launchd_plist.write_bytes(
            plistlib.dumps(
                MODULE._launchd_plist(
                    self.home,
                    "owner/private-sync",
                    23,
                    runner,
                    mode="private",
                    base_repo="owner/public-sync",
                    owner="private",
                ),
                sort_keys=True,
            )
        )

        config = MODULE._load_macos_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.platform, "macos")
        self.assertEqual(config.config_paths, (paths.launchd_plist,))
        self.assertEqual(config.interval_minutes, 23)
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.command, "run-scheduled")
        self.assertEqual(config.mode, "private")
        self.assertEqual(config.repo, "owner/private-sync")
        self.assertEqual(config.base_repo, "owner/public-sync")
        self.assertEqual(config.owner, "private")
        self.assertEqual(
            config.launchd_domain,
            MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
        )

    def test_linux_scheduler_config_parses_private_run_scheduled(self) -> None:
        runner = self.home / "bin" / "runner with spaces"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/private-sync",
                runner,
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(47),
            encoding="utf-8",
        )

        config = MODULE._load_linux_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.platform, "linux")
        self.assertEqual(
            config.config_paths,
            (paths.systemd_service, paths.systemd_timer),
        )
        self.assertEqual(config.interval_minutes, 47)
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.command, "run-scheduled")
        self.assertEqual(config.mode, "private")
        self.assertEqual(config.repo, "owner/private-sync")
        self.assertEqual(config.base_repo, "owner/public-sync")
        self.assertEqual(config.owner, "private")
        self.assertIsNone(config.launchd_domain)

    def test_macos_loader_accepts_only_exact_background_and_gui_profiles(
        self,
    ) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        profiles = (
            (
                MODULE._launchd_plist(
                    self.home,
                    "owner/public-sync",
                    19,
                    runner,
                ),
                MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
            ),
            (
                MODULE._legacy_gui_launchd_plist(
                    self.home,
                    "owner/public-sync",
                    19,
                    runner,
                ),
                MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
            ),
        )
        for payload, expected_domain in profiles:
            with self.subTest(domain=expected_domain):
                paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

                config = MODULE._load_macos_scheduler_config(paths)

                self.assertIsNotNone(config)
                assert config is not None
                self.assertEqual(config.launchd_domain, expected_domain)

        for mutation in ("unknown-key", "unknown-session", "unknown-process"):
            with self.subTest(mutation=mutation):
                payload = MODULE._launchd_plist(
                    self.home,
                    "owner/public-sync",
                    19,
                    runner,
                )
                if mutation == "unknown-key":
                    payload["KeepAlive"] = True
                elif mutation == "unknown-session":
                    payload["LimitLoadToSessionType"] = "Aqua"
                else:
                    payload["ProcessType"] = "Interactive"
                paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "unsupported execution semantics",
                ):
                    MODULE._load_macos_scheduler_config(paths)

        for field, value in (
            ("LowPriorityIO", 1),
            ("ThrottleInterval", 60.0),
        ):
            with self.subTest(type_confusion=field):
                payload = MODULE._launchd_plist(
                    self.home,
                    "owner/public-sync",
                    19,
                    runner,
                )
                payload[field] = value
                paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "unsupported execution semantics",
                ):
                    MODULE._load_macos_scheduler_config(paths)

    def test_macos_loader_accepts_no_bytecode_legacy_variant_only_for_migration(
        self,
    ) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        profiles = (
            (
                MODULE._launchd_plist,
                MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
            ),
            (
                MODULE._legacy_gui_launchd_plist,
                MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
            ),
        )
        commands = (
            (
                "install",
                [
                    str(runner),
                    "install",
                    "--repo",
                    "owner/public-sync",
                    "--home",
                    str(self.home),
                ],
                "owner/public-sync",
                "public",
            ),
            (
                "install-private",
                [
                    str(runner),
                    "install-private",
                    "--repo",
                    "owner/private-sync",
                    "--base-repo",
                    "owner/public-sync",
                    "--owner",
                    "private",
                    "--home",
                    str(self.home),
                ],
                "owner/private-sync",
                "private",
            ),
        )
        for builder, expected_domain in profiles:
            for command, arguments, repo, mode in commands:
                with self.subTest(domain=expected_domain, command=command):
                    payload = builder(
                        self.home,
                        repo,
                        19,
                        runner,
                        mode=mode,
                        base_repo="owner/public-sync",
                        owner="private",
                    )
                    payload["ProgramArguments"] = arguments
                    del payload["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"]
                    paths.launchd_plist.write_bytes(
                        plistlib.dumps(payload, sort_keys=True)
                    )

                    config = MODULE._load_macos_scheduler_config(paths)

                    self.assertIsNotNone(config)
                    assert config is not None
                    self.assertEqual(config.command, command)
                    self.assertEqual(config.launchd_domain, expected_domain)

        for builder, expected_domain in profiles:
            with self.subTest(
                domain=expected_domain,
                command="run-scheduled",
            ):
                payload = builder(
                    self.home,
                    "owner/public-sync",
                    19,
                    runner,
                )
                del payload["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"]
                paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "unsupported execution semantics",
                ):
                    MODULE._load_macos_scheduler_config(paths)

    def test_scheduler_config_read_tolerates_mtime_only_churn(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(37),
            encoding="utf-8",
        )
        real_read = MODULE.os.read
        touched = False

        def read_then_touch(file_fd: int, size: int) -> bytes:
            nonlocal touched
            payload = real_read(file_fd, size)
            if not payload and not touched:
                touched = True
                metadata = paths.systemd_service.stat()
                os.utime(
                    paths.systemd_service,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000_000,
                    ),
                )
            return payload

        with mock.patch.object(MODULE.os, "read", side_effect=read_then_touch):
            config = MODULE._load_linux_scheduler_config(paths)

        self.assertTrue(touched)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.interval_minutes, 37)

    def test_scheduler_config_read_rejects_same_inode_byte_drift(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        original = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        )
        paths.systemd_service.write_text(original, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(37),
            encoding="utf-8",
        )
        real_read = MODULE.os.read
        mutated = False

        def read_then_mutate(file_fd: int, size: int) -> bytes:
            nonlocal mutated
            payload = real_read(file_fd, size)
            if not payload and not mutated:
                mutated = True
                paths.systemd_service.write_text(
                    original.replace("Type=oneshot", "Type=onefail"),
                    encoding="utf-8",
                )
            return payload

        with (
            mock.patch.object(MODULE.os, "read", side_effect=read_then_mutate),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "content changed during read",
            ),
        ):
            MODULE._load_linux_scheduler_config(paths)

        self.assertTrue(mutated)

    def test_scheduler_install_args_use_stable_run_scheduled_entrypoint(self) -> None:
        runner = Path("/opt/codex/bin/codex-personal-sync")

        public_args = MODULE._scheduler_install_args(
            runner,
            "owner/public-sync",
            self.home,
        )
        private_args = MODULE._scheduler_install_args(
            runner,
            "owner/private-sync",
            self.home,
            mode="private",
            base_repo="owner/public-sync",
            owner="private",
        )

        self.assertEqual(
            public_args,
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "public",
                "--repo",
                "owner/public-sync",
                "--home",
                str(self.home),
            ],
        )
        self.assertEqual(
            private_args,
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "private",
                "--repo",
                "owner/private-sync",
                "--base-repo",
                "owner/public-sync",
                "--owner",
                "private",
                "--home",
                str(self.home),
            ],
        )

    def test_install_preserves_audited_existing_interval_when_omitted(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        legacy_payload = MODULE._launchd_plist(
            self.home,
            "owner/old-sync",
            17,
            runner,
        )
        legacy_payload["ProgramArguments"] = [
            str(runner),
            "install",
            "--repo",
            "owner/old-sync",
            "--home",
            str(self.home),
        ]
        paths.launchd_plist.write_bytes(plistlib.dumps(legacy_payload, sort_keys=True))

        self.install_scheduler_quietly(
            "owner/new-sync",
            None,
            "macos",
        )

        with paths.launchd_plist.open("rb") as stream:
            installed = plistlib.load(stream)
        self.assertEqual(installed["StartInterval"], 17 * 60)
        self.assertEqual(
            installed["ProgramArguments"],
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "public",
                "--repo",
                "owner/new-sync",
                "--home",
                str(self.home),
            ],
        )

    def test_exact_audited_config_avoids_reinstall_and_rewrite(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            29,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        before = (
            paths.systemd_service.read_bytes(),
            paths.systemd_timer.read_bytes(),
            paths.systemd_service.stat().st_mtime_ns,
            paths.systemd_timer.stat().st_mtime_ns,
        )

        output = io.StringIO()
        with (
            mock.patch.object(MODULE, "_write_text") as write_text,
            mock.patch.object(MODULE, "_run_native_command") as run_native,
            contextlib.redirect_stdout(output),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                None,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        write_text.assert_not_called()
        self.assertEqual(
            run_native.call_args_list,
            [
                mock.call(
                    ["systemctl", "--user", "daemon-reload"],
                    dry_run=False,
                    allow_fail=False,
                ),
                mock.call(
                    [
                        "systemctl",
                        "--user",
                        "start",
                        f"{MODULE.SYSTEMD_UNIT}.timer",
                    ],
                    dry_run=False,
                    allow_fail=False,
                ),
            ],
        )
        enablement = MODULE._systemd_timer_enablement_path(paths)
        self.assertEqual(os.readlink(enablement), str(paths.systemd_timer))
        self.assertIn("already matches audited configuration", output.getvalue())
        self.assertIn("preserved 29-minute interval", output.getvalue())
        self.assertEqual(
            (
                paths.systemd_service.read_bytes(),
                paths.systemd_timer.read_bytes(),
                paths.systemd_service.stat().st_mtime_ns,
                paths.systemd_timer.stat().st_mtime_ns,
            ),
            before,
        )

    def test_active_scheduler_change_without_enable_requires_reactivation(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                with mock.patch.object(MODULE, "_run_native_command"):
                    self.install_scheduler_quietly(
                        f"owner/{platform_name}-sync",
                        17,
                        platform_name,
                        enable=True,
                    )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                marker = MODULE._scheduler_activation_transaction_path(paths)
                self.assertFalse(marker.exists())

                self.install_scheduler_quietly(
                    f"owner/{platform_name}-sync",
                    29,
                    platform_name,
                    enable=False,
                )
                self.assertTrue(marker.exists())
                config_paths = tuple(
                    path
                    for path in (
                        paths.launchd_plist,
                        paths.systemd_service,
                        paths.systemd_timer,
                    )
                    if path is not None
                )
                before = tuple(
                    (
                        path.read_bytes(),
                        path.stat().st_dev,
                        path.stat().st_ino,
                        path.stat().st_mtime_ns,
                    )
                    for path in config_paths
                )

                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ) as daemon_enabled:
                    report = MODULE.scheduler_report(self.home, platform_name)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _doctor_report, issues = MODULE.doctor(
                            self.home,
                            platform_name,
                            json_output=False,
                        )
                daemon_enabled.assert_not_called()
                self.assertTrue(report.installed)
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertIsNotNone(report.daemon_query)
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )
                self.assertNotIn(
                    "scheduler-daemon-unavailable",
                    {code for code, _reason in report.failures},
                )
                self.assertTrue(
                    any(
                        issue.code == "scheduler-activation-incomplete"
                        and issue.path == marker
                        for issue in issues
                    )
                )

                writer_name = (
                    "_write_plist" if platform_name == "macos" else "_write_text"
                )
                with (
                    mock.patch.object(MODULE, writer_name) as writer,
                    mock.patch.object(MODULE, "_run_native_command"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-sync",
                        None,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                writer.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual(
                    tuple(
                        (
                            path.read_bytes(),
                            path.stat().st_dev,
                            path.stat().st_ino,
                            path.stat().st_mtime_ns,
                        )
                        for path in config_paths
                    ),
                    before,
                )
                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ):
                    recovered = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertTrue(recovered.enabled)
                self.assertNotEqual(
                    recovered.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertEqual(recovered.interval_minutes, 29)

    def test_activation_failure_persists_marker_until_exact_retry(self) -> None:
        self.write_runner()
        failure_actions = {
            "macos": "bootstrap",
            "linux": "daemon-reload",
        }
        for platform_name, failure_action in failure_actions.items():
            with self.subTest(platform=platform_name):
                paths = MODULE._scheduler_paths(platform_name, self.home)
                marker = MODULE._scheduler_activation_transaction_path(paths)

                def fail_activation(
                    args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool | str = False,
                ) -> None:
                    del dry_run, allow_fail
                    if failure_action in args:
                        raise MODULE.SyncError(f"simulated {failure_action} failure")

                with (
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=fail_activation,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        f"simulated {failure_action} failure",
                    ),
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-failure",
                        31,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                self.assertTrue(marker.exists())
                config_paths = tuple(
                    path
                    for path in (
                        paths.launchd_plist,
                        paths.systemd_service,
                        paths.systemd_timer,
                    )
                    if path is not None
                )
                before = tuple(
                    (
                        path.read_bytes(),
                        path.stat().st_dev,
                        path.stat().st_ino,
                        path.stat().st_mtime_ns,
                    )
                    for path in config_paths
                )
                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ) as daemon_enabled:
                    report = MODULE.scheduler_report(self.home, platform_name)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _doctor_report, issues = MODULE.doctor(
                            self.home,
                            platform_name,
                            json_output=False,
                        )
                daemon_enabled.assert_not_called()
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertTrue(
                    any(
                        issue.code == "scheduler-activation-incomplete"
                        and issue.path == marker
                        for issue in issues
                    )
                )

                writer_name = (
                    "_write_plist" if platform_name == "macos" else "_write_text"
                )
                with (
                    mock.patch.object(MODULE, writer_name) as writer,
                    mock.patch.object(MODULE, "_run_native_command"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-failure",
                        None,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                writer.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual(
                    tuple(
                        (
                            path.read_bytes(),
                            path.stat().st_dev,
                            path.stat().st_ino,
                            path.stat().st_mtime_ns,
                        )
                        for path in config_paths
                    ),
                    before,
                )
                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ):
                    recovered = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertTrue(recovered.enabled)
                self.assertEqual(recovered.interval_minutes, 31)

    def test_macos_activation_retry_enables_background_label_before_bootstrap(
        self,
    ) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        marker = MODULE._scheduler_activation_transaction_path(paths)
        uid = os.getuid()
        gui_target = f"{MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN}/{uid}"
        background_target = f"{MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN}/{uid}"
        label = MODULE.LAUNCHD_LABEL
        failed_calls: list[list[str]] = []

        def fail_bootstrap(
            args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool | str = False,
        ) -> None:
            del dry_run, allow_fail
            failed_calls.append(args)
            if args[:2] == ["launchctl", "bootstrap"]:
                raise MODULE.SyncError("simulated bootstrap failure")

        with (
            mock.patch.object(MODULE, "LEGACY_LAUNCHD_LABELS", ()),
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=fail_bootstrap,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "simulated bootstrap failure",
            ),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/macos-activation-order",
                31,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            failed_calls,
            [
                ["launchctl", "bootout", f"{gui_target}/{label}"],
                ["launchctl", "disable", f"{gui_target}/{label}"],
                ["launchctl", "bootout", f"{background_target}/{label}"],
                ["launchctl", "enable", f"{background_target}/{label}"],
                [
                    "launchctl",
                    "bootstrap",
                    background_target,
                    str(paths.launchd_plist),
                ],
            ],
        )
        self.assertTrue(marker.exists())
        config_before = (
            paths.launchd_plist.read_bytes(),
            paths.launchd_plist.stat().st_dev,
            paths.launchd_plist.stat().st_ino,
        )
        retry_calls: list[list[str]] = []

        def record_retry(
            args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool | str = False,
        ) -> None:
            del dry_run, allow_fail
            retry_calls.append(args)

        with (
            mock.patch.object(MODULE, "LEGACY_LAUNCHD_LABELS", ()),
            mock.patch.object(MODULE, "_write_plist") as write_plist,
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=record_retry,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/macos-activation-order",
                None,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        write_plist.assert_not_called()
        self.assertEqual(
            retry_calls,
            [
                ["launchctl", "bootout", f"{gui_target}/{label}"],
                ["launchctl", "disable", f"{gui_target}/{label}"],
                ["launchctl", "bootout", f"{background_target}/{label}"],
                ["launchctl", "enable", f"{background_target}/{label}"],
                [
                    "launchctl",
                    "bootstrap",
                    background_target,
                    str(paths.launchd_plist),
                ],
                ["launchctl", "enable", f"{background_target}/{label}"],
            ],
        )
        self.assertFalse(marker.exists())
        self.assertEqual(
            (
                paths.launchd_plist.read_bytes(),
                paths.launchd_plist.stat().st_dev,
                paths.launchd_plist.stat().st_ino,
            ),
            config_before,
        )

    def test_invalid_activation_state_fails_closed_and_uninstall_clears_it(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    f"owner/{platform_name}-invalid",
                    23,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                marker = MODULE._scheduler_activation_transaction_path(paths)
                marker.write_text("{invalid\n", encoding="utf-8")

                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ) as daemon_enabled:
                    report = MODULE.scheduler_report(self.home, platform_name)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _doctor_report, issues = MODULE.doctor(
                            self.home,
                            platform_name,
                            json_output=False,
                        )
                daemon_enabled.assert_not_called()
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-state-invalid",
                )
                self.assertTrue(
                    any(
                        issue.code == "scheduler-activation-state-invalid"
                        and issue.path == marker
                        for issue in issues
                    )
                )

                writer_name = (
                    "_write_plist" if platform_name == "macos" else "_write_text"
                )
                with (
                    mock.patch.object(MODULE, writer_name) as writer,
                    mock.patch.object(MODULE, "_run_native_command") as native,
                    self.assertRaises(MODULE.SyncError) as raised,
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-invalid",
                        37,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "scheduler-activation-state-invalid",
                )
                writer.assert_not_called()
                native.assert_not_called()

                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_scheduler(
                        self.home,
                        platform_name,
                        dry_run=False,
                        disable=False,
                    )
                self.assertFalse(marker.exists())

    def test_status_report_includes_config_releases_and_runtime_health(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/private-sync",
                runner,
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(31),
            encoding="utf-8",
        )
        runtime_state = {
            "version": 1,
            "last_attempt": "2026-07-23T09:15:00+00:00",
            "last_success": "2026-07-23T08:15:00+00:00",
            "success": False,
            "failure_reason": "network unavailable",
            "mode": "private",
            "repo": "owner/private-sync",
            "base_repo": "owner/public-sync",
            "owner": "private",
        }
        output = io.StringIO()

        with (
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(
                    (MODULE.PUBLIC_OWNER, PUBLIC_SHA),
                    ("private", PRIVATE_SHA),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            contextlib.redirect_stdout(output),
        ):
            report = MODULE.status_scheduler(
                self.home,
                "linux",
                json_output=True,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(report.platform, "linux")
        self.assertTrue(report.installed)
        self.assertEqual(
            payload,
            {
                "platform": "linux",
                "installed": True,
                "enabled": True,
                "config": [
                    str(paths.systemd_service),
                    str(paths.systemd_timer),
                ],
                "interval_minutes": 31,
                "runner": str(runner),
                "stable_runner": False,
                "command": "run-scheduled",
                "mode": "private",
                "repo": "owner/private-sync",
                "base_repo": "owner/public-sync",
                "private_repo": "owner/private-sync",
                "owner": "private",
                "migration_needed": False,
                "last_attempt": "2026-07-23T09:15:00+00:00",
                "recent_success": "2026-07-23T08:15:00+00:00",
                "current_release": {
                    MODULE.PUBLIC_OWNER: PUBLIC_SHA,
                    "private": PRIVATE_SHA,
                },
                "release_integrity": [],
                "quarantine_batches": 0,
                "quarantine_limit": MODULE.MAX_RETAINED_QUARANTINE_BATCHES,
                "mirror_quarantine": MODULE._mirror_quarantine_payload(
                    report.mirror_quarantine
                ),
                "failure_code": None,
                "failure_reason": "network unavailable",
                "daemon_query": {
                    "classification": "enabled",
                    "reason": None,
                },
                "failures": [
                    {
                        "code": None,
                        "reason": "network unavailable",
                    },
                    {
                        "code": "scheduler-runner-drift",
                        "reason": (
                            "scheduler does not use the stable installed runner path"
                        ),
                    },
                ],
            },
        )

    def test_status_reports_reconstructable_legacy_private_migration(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        service_lines = MODULE._systemd_service(
            self.home,
            "owner/private-sync",
            runner,
            mode="private",
            base_repo="owner/public-sync",
            owner="private",
        ).splitlines()
        legacy_arguments = [
            str(runner),
            "install-private",
            "--repo",
            "owner/private-sync",
            "--base-repo",
            "owner/public-sync",
            "--owner",
            "private",
            "--home",
            str(self.home),
        ]
        legacy_exec = " ".join(
            MODULE._systemd_quote(argument) for argument in legacy_arguments
        )
        service_lines = [
            f"ExecStart={legacy_exec}" if line.startswith("ExecStart=") else line
            for line in service_lines
        ]
        paths.systemd_service.write_text(
            "\n".join(service_lines),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(MODULE._systemd_timer(47), encoding="utf-8")

        with (
            mock.patch.object(
                MODULE, "_read_scheduler_runtime_state", return_value=None
            ),
            mock.patch.object(
                MODULE, "_current_releases_for_scheduler", return_value=()
            ),
            mock.patch.object(MODULE, "_scheduler_daemon_enabled", return_value=False),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        payload = MODULE._scheduler_report_payload(report)
        self.assertEqual(payload["command"], "install-private")
        self.assertEqual(payload["mode"], "private")
        self.assertEqual(payload["repo"], "owner/private-sync")
        self.assertEqual(payload["base_repo"], "owner/public-sync")
        self.assertEqual(payload["owner"], "private")
        self.assertEqual(payload["interval_minutes"], 47)
        self.assertTrue(payload["migration_needed"])

    def test_status_preserves_legal_legacy_scheduler_migrations(self) -> None:
        runner = self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.write_legacy_scheduler_config(
                    platform_name,
                    runner,
                    include_mode=False,
                )
                status, report, daemon_query = self.scheduler_status(
                    platform_name,
                    daemon_enabled=True,
                    strict=False,
                )

                self.assertEqual(status, 0, report)
                self.assertTrue(report["installed"])
                self.assertTrue(report["migration_needed"])
                self.assertNotEqual(
                    report["failure_code"],
                    "scheduler-config-invalid",
                )
                daemon_query.assert_called_once()

    def test_legacy_mode_flag_is_invalid_for_loaders_and_strict_status(self) -> None:
        runner = self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                paths, loader, command = self.write_legacy_scheduler_config(
                    platform_name,
                    runner,
                    include_mode=True,
                )

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    re.escape(f"command {command} does not allow --mode"),
                ):
                    loader(paths)

                status, report, daemon_query = self.scheduler_status(
                    platform_name,
                    daemon_enabled=True,
                    strict=True,
                )

                daemon_query.assert_not_called()
                self.assertEqual(status, 1)
                self.assertFalse(report["installed"])
                self.assertEqual(report["failure_code"], "scheduler-config-invalid")
                self.assertIn("does not allow --mode", report["failure_reason"])

    def test_linux_option_like_value_is_invalid_for_loader_and_strict_status(
        self,
    ) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        arguments = [
            str(runner),
            "run-scheduled",
            "--mode",
            "public",
            "--repo",
            "owner/public-sync",
            "--home",
            "--unexpected",
        ]
        exec_start = " ".join(
            MODULE._systemd_quote(argument) for argument in arguments
        )
        service_lines = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        ).splitlines()
        paths.systemd_service.write_text(
            "\n".join(
                f"ExecStart={exec_start}"
                if line.startswith("ExecStart=")
                else line
                for line in service_lines
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(MODULE._systemd_timer(17), encoding="utf-8")

        expected = "scheduler config value for --home must not be an option token"
        with self.assertRaisesRegex(MODULE.SyncError, re.escape(expected)):
            MODULE._load_linux_scheduler_config(paths)

        status, report, daemon_query = self.scheduler_status(
            "linux",
            daemon_enabled=True,
            strict=True,
        )

        daemon_query.assert_not_called()
        self.assertEqual(status, 1)
        self.assertFalse(report["installed"])
        self.assertEqual(report["failure_code"], "scheduler-config-invalid")
        self.assertEqual(report["failure_reason"], expected)

    def test_macos_status_marks_gui_domain_for_background_migration(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        profiles = (
            (
                MODULE._launchd_plist(
                    self.home,
                    "owner/public-sync",
                    17,
                    runner,
                ),
                False,
            ),
            (
                MODULE._legacy_gui_launchd_plist(
                    self.home,
                    "owner/public-sync",
                    17,
                    runner,
                ),
                True,
            ),
        )
        for payload, expected_migration in profiles:
            with self.subTest(migration=expected_migration):
                paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
                with (
                    mock.patch.object(
                        MODULE,
                        "_read_scheduler_runtime_state",
                        return_value=None,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_current_releases_for_scheduler",
                        return_value=(),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        return_value=True,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_release_integrity_issues",
                        return_value=(),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_quarantine_batch_count",
                        return_value=0,
                    ),
                ):
                    report = MODULE.scheduler_report(self.home, "macos")

                self.assertEqual(report.migration_needed, expected_migration)

    def test_run_scheduled_persists_success_state(self) -> None:
        with (
            mock.patch.object(MODULE, "install_from_github") as install,
            mock.patch.object(
                MODULE,
                "_capture_scheduler_release_trees",
                return_value={},
            ),
            mock.patch.object(
                MODULE,
                "_write_scheduler_runtime_state",
                wraps=MODULE._write_scheduler_runtime_state,
            ) as write_state,
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/public-sync",
                mode="public",
                base_repo="owner/ignored-base",
                owner="private",
            )

        install.assert_called_once_with(
            "owner/public-sync",
            self.home,
            dry_run=False,
        )
        self.assertEqual(write_state.call_count, 2)
        initial = write_state.call_args_list[0].args[1]
        final = write_state.call_args_list[1].args[1]
        self.assertFalse(initial["success"])
        self.assertEqual(
            initial["failure_reason"],
            "scheduled sync did not complete",
        )
        self.assertTrue(final["success"])
        self.assertIsNone(final["failure_reason"])
        self.assertEqual(final["last_attempt"], final["last_success"])
        self.assertEqual(final["mode"], "public")
        self.assertEqual(final["repo"], "owner/public-sync")
        self.assertEqual(final["base_repo"], "owner/public-sync")
        self.assertEqual(final["owner"], MODULE.PUBLIC_OWNER)
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            final,
        )
        self.assertEqual(
            stat.S_IMODE(MODULE._scheduler_status_path(self.home).stat().st_mode),
            0o600,
        )

    def test_run_scheduled_persists_failure_and_previous_success(self) -> None:
        previous_success = "2026-07-22T08:00:00+00:00"
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 1,
                "last_attempt": previous_success,
                "last_success": previous_success,
                "success": True,
                "failure_reason": None,
                "mode": "private",
                "repo": "owner/private-sync",
                "base_repo": "owner/public-sync",
                "owner": "private",
            },
        )

        with (
            mock.patch.object(
                MODULE,
                "install_private_from_github",
                side_effect=MODULE.SyncError("network unavailable"),
            ) as install,
            mock.patch.object(
                MODULE,
                "_write_scheduler_runtime_state",
                wraps=MODULE._write_scheduler_runtime_state,
            ) as write_state,
            self.assertRaisesRegex(MODULE.SyncError, "network unavailable"),
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/private-sync",
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            )

        install.assert_called_once_with(
            "owner/private-sync",
            self.home,
            base_repo="owner/public-sync",
            owner="private",
            dry_run=False,
        )
        self.assertEqual(write_state.call_count, 2)
        initial = write_state.call_args_list[0].args[1]
        final = write_state.call_args_list[1].args[1]
        self.assertFalse(initial["success"])
        self.assertEqual(initial["last_success"], previous_success)
        self.assertFalse(final["success"])
        self.assertEqual(final["last_success"], previous_success)
        self.assertEqual(final["failure_reason"], "network unavailable")
        self.assertEqual(final["mode"], "private")
        self.assertEqual(final["repo"], "owner/private-sync")
        self.assertEqual(final["base_repo"], "owner/public-sync")
        self.assertEqual(final["owner"], "private")
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            final,
        )

    def test_overlapping_scheduled_completion_cannot_replace_newer_failure(
        self,
    ) -> None:
        older_install_entered = threading.Event()
        release_older_install = threading.Event()
        errors: dict[str, BaseException] = {}

        def interleaved_install(
            repo: str,
            home: Path,
            *,
            dry_run: bool,
        ) -> None:
            self.assertEqual(repo, "owner/public-sync")
            self.assertEqual(home, self.home)
            self.assertFalse(dry_run)
            if threading.current_thread().name == "older-scheduled-run":
                older_install_entered.set()
                if not release_older_install.wait(5):
                    raise AssertionError("older scheduled run was not released")
                return
            raise MODULE.SyncError(
                "newer scheduled run failed",
                code="newer-attempt-failed",
            )

        def run(name: str) -> None:
            try:
                MODULE.run_scheduled(
                    self.home,
                    "owner/public-sync",
                    mode="public",
                    base_repo="owner/ignored",
                    owner="private",
                )
            except BaseException as error:
                errors[name] = error

        older = threading.Thread(
            target=run,
            args=("older",),
            name="older-scheduled-run",
            daemon=True,
        )
        newer = threading.Thread(
            target=run,
            args=("newer",),
            name="newer-scheduled-run",
            daemon=True,
        )
        with (
            mock.patch.object(
                MODULE,
                "install_from_github",
                side_effect=interleaved_install,
            ),
            mock.patch.object(
                MODULE,
                "_capture_scheduler_release_trees",
                return_value={},
            ),
        ):
            try:
                older.start()
                self.assertTrue(older_install_entered.wait(5))
                older_incomplete = MODULE._read_scheduler_runtime_state(self.home)
                assert older_incomplete is not None

                newer.start()
                newer.join(5)
                self.assertFalse(newer.is_alive())
                newer_failure = MODULE._read_scheduler_runtime_state(self.home)
                assert newer_failure is not None
                self.assertIsInstance(errors.get("newer"), MODULE.SyncError)
                self.assertFalse(newer_failure["success"])
                self.assertEqual(
                    newer_failure["failure_code"],
                    "newer-attempt-failed",
                )
                self.assertGreater(
                    datetime.fromisoformat(newer_failure["last_attempt"]),
                    datetime.fromisoformat(older_incomplete["last_attempt"]),
                )
            finally:
                release_older_install.set()
                if older.ident is not None:
                    older.join(5)
                if newer.ident is not None:
                    newer.join(5)
            self.assertFalse(older.is_alive())

        self.assertNotIn("older", errors)
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            newer_failure,
        )

    def test_superseded_scheduled_install_cannot_roll_back_newer_release(
        self,
    ) -> None:
        older_sha = "3" * 40
        newer_sha = "4" * 40
        older_release = self.root / "older-release"
        newer_release = self.root / "newer-release"

        def write_release(root: Path, payload: str) -> None:
            skill_root = root / "personal_codex" / "skills" / "scheduler-race"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(payload, encoding="utf-8")
            (root / "personal_codex" / "sync-manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "source": "personal_codex/skills/scheduler-race",
                                "target": "skills/scheduler-race",
                                "kind": "skill",
                            }
                        ],
                        "reference_only": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        write_release(older_release, "---\nname: scheduler-race\nold: true\n---\n")
        write_release(newer_release, "---\nname: scheduler-race\nnew: true\n---\n")
        older_download_entered = threading.Event()
        release_older_download = threading.Event()
        errors: dict[str, BaseException] = {}

        def downloaded_release(root: Path, sha: str) -> MODULE.DownloadedRelease:
            return MODULE.DownloadedRelease(
                repo="owner/public-sync",
                assets=MODULE.ReleaseAssets(
                    tag_name=f"personal-codex-20260723-120000-{sha[:7]}",
                    sha=sha,
                    archive_name=f"personal-codex-{sha}.tar.gz",
                    archive_id=1,
                    archive_size=1,
                    checksum_name=f"personal-codex-{sha}.sha256",
                    checksum_id=2,
                    checksum_size=1,
                ),
                release_root=root,
            )

        def interleaved_download(
            repo: str,
            destination: Path,
            *,
            workspace,
            sha: str | None = None,
        ) -> MODULE.DownloadedRelease:
            del destination, workspace, sha
            self.assertEqual(repo, "owner/public-sync")
            if threading.current_thread().name == "older-release-run":
                older_download_entered.set()
                if not release_older_download.wait(5):
                    raise AssertionError("older release download was not resumed")
                return downloaded_release(older_release, older_sha)
            return downloaded_release(newer_release, newer_sha)

        def run(name: str) -> None:
            try:
                MODULE.run_scheduled(
                    self.home,
                    "owner/public-sync",
                    mode="public",
                    base_repo="owner/ignored",
                    owner="private",
                )
            except BaseException as error:
                errors[name] = error

        older = threading.Thread(
            target=run,
            args=("older",),
            name="older-release-run",
            daemon=True,
        )
        newer = threading.Thread(
            target=run,
            args=("newer",),
            name="newer-release-run",
            daemon=True,
        )
        with mock.patch.object(
            MODULE,
            "download_and_extract_release",
            side_effect=interleaved_download,
        ):
            try:
                older.start()
                self.assertTrue(older_download_entered.wait(5))
                newer.start()
                newer.join(10)
                self.assertFalse(newer.is_alive())
                self.assertNotIn("newer", errors)
                self.assertEqual(
                    MODULE._current_sha(self.home, MODULE.PUBLIC_OWNER),
                    newer_sha,
                )
                newer_state = MODULE._read_scheduler_runtime_state(self.home)
                assert newer_state is not None
                self.assertTrue(newer_state["success"])
                self.assertEqual(
                    newer_state["release_trees"][MODULE.PUBLIC_OWNER]["sha"],
                    newer_sha,
                )
            finally:
                release_older_download.set()
                if older.ident is not None:
                    older.join(10)
                if newer.ident is not None:
                    newer.join(10)

        self.assertFalse(older.is_alive())
        self.assertIsInstance(errors.get("older"), MODULE.SyncError)
        assert isinstance(errors["older"], MODULE.SyncError)
        self.assertEqual(errors["older"].code, "scheduled-sync-superseded")
        self.assertEqual(
            MODULE._current_sha(self.home, MODULE.PUBLIC_OWNER),
            newer_sha,
        )
        self.assertFalse(
            (self.home / "personal-sync" / "releases" / older_sha).exists()
        )
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            newer_state,
        )

    def test_scheduler_attempt_recovers_from_unbounded_or_noncanonical_time(
        self,
    ) -> None:
        invalid_attempts = (
            "9999-12-31T23:59:59.999999+00:00",
            "9999-12-31T23:59:59.999999-23:59",
            "2026-07-23T09:15:00Z",
            "2026-07-23T09:15:00",
            "not-a-timestamp",
        )

        for previous_attempt in invalid_attempts:
            with self.subTest(previous_attempt=previous_attempt):
                attempt = MODULE._next_scheduler_attempt(
                    {"last_attempt": previous_attempt}
                )
                parsed = datetime.fromisoformat(attempt)
                self.assertEqual(parsed.utcoffset(), timedelta(0))
                self.assertEqual(parsed.isoformat(), attempt)
                self.assertNotEqual(attempt, previous_attempt)
                self.assertLess(parsed.year, 9999)

    def test_scheduler_attempt_rejects_far_future_but_preserves_cas_order(
        self,
    ) -> None:
        far_future = (
            datetime.now(timezone.utc)
            + MODULE.MAX_SCHEDULER_ATTEMPT_FUTURE_SKEW
            + timedelta(days=1)
        ).isoformat()
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 2,
                "last_attempt": far_future,
                "last_success": far_future,
                "success": True,
                "failure_reason": "injected future state",
                "failure_code": "injected-future-state",
                "release_trees": {
                    MODULE.PUBLIC_OWNER: {
                        "sha": PUBLIC_SHA,
                        "tree_sha256": "a" * 64,
                    }
                },
                "mode": "public",
                "repo": "owner/public-sync",
                "base_repo": "owner/public-sync",
                "owner": MODULE.PUBLIC_OWNER,
            },
        )
        with self.assertRaises(MODULE.SyncError) as invalid_state:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            invalid_state.exception.code,
            "scheduler-state-timestamp-invalid",
        )

        replacement_attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertNotEqual(replacement_attempt, far_future)
        self.assertFalse(
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )
        )
        state = MODULE._read_scheduler_runtime_state(self.home)
        assert state is not None
        self.assertEqual(state["last_attempt"], replacement_attempt)
        self.assertIsNone(state["last_success"])
        self.assertEqual(state["release_trees"], {})

    def test_scheduler_runtime_cas_preserves_identity_replacement(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        replacement = b"foreign newer scheduler state\n"
        real_publish = MODULE._atomic_write_scheduler_config

        def replace_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.unlink()
            status_path.write_bytes(replacement)
            status_path.chmod(0o600)
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=replace_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), replacement)

    def test_scheduler_runtime_cas_does_not_restore_unproven_late_replacement(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        newer = b"foreign state that won the late race\n"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0
        reader_blocked = False

        def replace_live_then_exchange(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count, reader_blocked
            exchange_count += 1
            if exchange_count == 1:
                try:
                    MODULE._read_scheduler_runtime_state(self.home)
                except MODULE.SyncError as error:
                    reader_blocked = (
                        error.code == "scheduler-state-publication-incomplete"
                    )
                else:
                    self.fail(
                        "runtime reader accepted state while publication marker "
                        "was active"
                    )
                marker_path.unlink()
                status_path.unlink()
                status_path.write_bytes(newer)
                status_path.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=replace_live_then_exchange,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "could not prove that the temporary object is the exact state "
                "displaced",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(exchange_count, 1)
        self.assertTrue(reader_blocked)
        self.assertNotEqual(status_path.read_bytes(), newer)
        self.assertEqual(
            json.loads(status_path.read_text(encoding="utf-8"))["last_attempt"],
            attempt,
        )
        displaced_paths = [
            candidate
            for candidate in status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*"
            )
            if not candidate.name.endswith(".original")
        ]
        self.assertEqual(len(displaced_paths), 1)
        self.assertEqual(displaced_paths[0].read_bytes(), newer)
        self.assertTrue(marker_path.is_file())
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unresolved publication marker",
        ) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        report = MODULE.scheduler_report(self.home, "linux")
        self.assertEqual(
            report.failure_code,
            "scheduler-state-publication-incomplete",
        )
        self.assertIsNone(report.last_attempt)
        self.assertIsNone(report.recent_success)

    def test_scheduler_runtime_residue_blocks_when_marker_rebuild_fails(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        newer = b"foreign state that won the late race\n"
        real_create = MODULE._create_scheduler_runtime_publication_marker
        real_exchange = MODULE._rename_exchange_at
        create_calls = 0

        def create_once_then_fail(
            user_home: Path,
            path: Path,
            parent_fd: int,
            payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal create_calls
            create_calls += 1
            if create_calls == 1:
                return real_create(
                    user_home,
                    path,
                    parent_fd,
                    payload,
                )
            raise MODULE.SyncError("injected marker recreation failure")

        def delete_marker_and_replace_live(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            marker_path.unlink()
            status_path.unlink()
            status_path.write_bytes(newer)
            status_path.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_create_scheduler_runtime_publication_marker",
                side_effect=create_once_then_fail,
            ),
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=delete_marker_and_replace_live,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "marker recreation failure",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(create_calls, 3)
        self.assertFalse(marker_path.exists())
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_cas_never_swaps_unproven_temp_back_to_live(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original = status_path.read_bytes()
        attacker = b"attacker-controlled temporary replacement\n"
        saved_displaced = status_path.parent / "saved-displaced-status"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0

        def swap_displaced_temp(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count
            exchange_count += 1
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )
            if exchange_count != 1:
                return
            saved_displaced.write_bytes(attacker)
            saved_displaced.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                first_parent_fd,
                saved_displaced.name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=swap_displaced_temp,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "could not prove that the temporary object is the exact state "
                "displaced",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(exchange_count, 1)
        self.assertNotEqual(status_path.read_bytes(), attacker)
        self.assertEqual(
            json.loads(status_path.read_text(encoding="utf-8"))["last_attempt"],
            attempt,
        )
        recovery_paths = list(
            status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*.original"
            )
        )
        self.assertEqual(len(recovery_paths), 1)
        self.assertEqual(recovery_paths[0].read_bytes(), original)
        displaced_paths = [
            candidate
            for candidate in status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*"
            )
            if not candidate.name.endswith(".original")
        ]
        self.assertEqual(len(displaced_paths), 1)
        self.assertEqual(displaced_paths[0].read_bytes(), attacker)
        self.assertEqual(saved_displaced.read_bytes(), original)
        self.assertTrue(
            MODULE._scheduler_runtime_publication_marker_path(status_path).is_file()
        )

    def test_scheduler_runtime_reader_rejects_marker_appearing_after_read(
        self,
    ) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        real_check = MODULE._scheduler_runtime_publication_is_incomplete
        checks = 0

        def appear_on_post_check(
            home: Path,
            checked_status_path: Path,
            parent_fd: int,
        ) -> bool:
            nonlocal checks
            checks += 1
            if checks == 2:
                marker_path.write_text(
                    '{"version":1,"status":"incomplete"}\n',
                    encoding="utf-8",
                )
                marker_path.chmod(0o600)
            return real_check(home, checked_status_path, parent_fd)

        with (
            mock.patch.object(
                MODULE,
                "_scheduler_runtime_publication_is_incomplete",
                side_effect=appear_on_post_check,
            ),
            self.assertRaises(MODULE.SyncError) as error_context,
        ):
            MODULE._read_scheduler_runtime_state(self.home)

        self.assertEqual(checks, 2)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_reader_directly_stats_fixed_marker(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        marker_path.write_text(
            '{"version":1,"status":"incomplete"}\n',
            encoding="utf-8",
        )
        marker_path.chmod(0o600)
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with mock.patch.object(
                MODULE,
                "_directory_member_names",
                return_value=(status_path.name,),
            ):
                self.assertTrue(
                    MODULE._scheduler_runtime_publication_is_incomplete(
                        self.home,
                        status_path,
                        parent_fd,
                    )
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_casefold_aliases(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        transaction_prefix = f".{status_path.name}.personal-sync-write-"
        retained_marker_prefix = (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{marker_path.name}-"
        )
        retained_transaction_prefix = (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{transaction_prefix}"
        )
        aliases = (
            marker_path.name.swapcase(),
            f"{transaction_prefix.swapcase()}case-alias",
            f"{retained_marker_prefix.swapcase()}case-alias",
            f"{retained_transaction_prefix.swapcase()}case-alias",
        )
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            for alias in aliases:
                with (
                    self.subTest(alias=alias),
                    mock.patch.object(
                        MODULE,
                        "_directory_member_names",
                        return_value=(status_path.name, alias),
                    ),
                ):
                    self.assertTrue(
                        MODULE._scheduler_runtime_publication_is_incomplete(
                            self.home,
                            status_path,
                            parent_fd,
                        )
                    )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_nfd_residue_alias(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        canonical_status_path = MODULE._scheduler_status_path(self.home)
        status_path = canonical_status_path.with_name("scheduler-Café.json")
        nfd_residue = ".scheduler-Cafe\u0301.json.personal-sync-write-nfd-alias"
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with mock.patch.object(
                MODULE,
                "_directory_member_names",
                return_value=(canonical_status_path.name, nfd_residue),
            ):
                self.assertTrue(
                    MODULE._scheduler_runtime_publication_is_incomplete(
                        self.home,
                        status_path,
                        parent_fd,
                    )
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_normalized_alias_collision(
        self,
    ) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        canonical_status_path = MODULE._scheduler_status_path(self.home)
        status_path = canonical_status_path.with_name("scheduler-Café.json")
        nfc_residue = ".scheduler-Café.json.personal-sync-write-normalized-alias"
        nfd_residue = ".scheduler-Cafe\u0301.json.personal-sync-write-normalized-alias"
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_directory_member_names",
                    return_value=(
                        canonical_status_path.name,
                        nfc_residue,
                        nfd_residue,
                    ),
                ),
                self.assertRaises(MODULE.SyncError) as error_context,
            ):
                MODULE._scheduler_runtime_publication_is_incomplete(
                    self.home,
                    status_path,
                    parent_fd,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        self.assertIn(
            "ambiguous portable aliases",
            str(error_context.exception),
        )

    def test_scheduler_runtime_writer_refuses_preexisting_marker(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original = status_path.read_bytes()
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        marker_path.write_text(
            '{"version":1,"status":"incomplete"}\n',
            encoding="utf-8",
        )
        marker_path.chmod(0o600)

        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        self.assertEqual(status_path.read_bytes(), original)

    def test_scheduler_runtime_marker_cleanup_failure_remains_fail_closed(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        real_commit = MODULE._commit_scheduler_runtime_publication_marker
        injected = False
        reader_blocked = False

        def replace_marker_before_commit(
            user_home: Path,
            path: Path,
            parent_fd: int,
            expected: MODULE.ManagedStateFileSnapshot,
        ) -> None:
            nonlocal injected, reader_blocked
            if not injected:
                injected = True
                try:
                    MODULE._read_scheduler_runtime_state(self.home)
                except MODULE.SyncError as error:
                    reader_blocked = (
                        error.code == "scheduler-state-publication-incomplete"
                    )
                else:
                    self.fail(
                        "runtime reader accepted state before the marker commit "
                        "linearization point"
                    )
                marker_path.unlink()
                marker_path.write_bytes(b"foreign marker replacement\n")
                marker_path.chmod(0o600)
            real_commit(
                user_home,
                path,
                parent_fd,
                expected,
            )

        with (
            mock.patch.object(
                MODULE,
                "_commit_scheduler_runtime_publication_marker",
                side_effect=replace_marker_before_commit,
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertTrue(injected)
        self.assertTrue(reader_blocked)
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        self.assertTrue(marker_path.exists())
        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_cas_rejects_same_inode_content_and_access_drift(
        self,
    ) -> None:
        for drift in ("content", "access"):
            with self.subTest(drift=drift):
                case_home = self.home / f"runtime-{drift}"
                case_home.mkdir()
                attempt = MODULE._begin_scheduler_attempt(
                    case_home,
                    mode="public",
                    repo="owner/public-sync",
                    base_repo="owner/public-sync",
                    owner=MODULE.PUBLIC_OWNER,
                )
                status_path = MODULE._scheduler_status_path(case_home)
                original_identity = (
                    status_path.stat().st_dev,
                    status_path.stat().st_ino,
                )
                real_publish = MODULE._atomic_write_scheduler_config

                def drift_before_publish(
                    path: Path,
                    payload: bytes,
                    *,
                    expected_snapshot: (MODULE.ManagedStateFileSnapshot | None) = None,
                    mode: int = 0o600,
                    gid: int | None = None,
                    rollback_displaced_conflict: bool = False,
                ) -> None:
                    if drift == "content":
                        with status_path.open("r+b") as stream:
                            stream.seek(0)
                            stream.write(b"foreign same-inode state\n")
                            stream.truncate()
                            stream.flush()
                            os.fsync(stream.fileno())
                    else:
                        status_path.chmod(0o640)
                    real_publish(
                        path,
                        payload,
                        expected_snapshot=expected_snapshot,
                        mode=mode,
                        gid=gid,
                        rollback_displaced_conflict=rollback_displaced_conflict,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_atomic_write_scheduler_config",
                        side_effect=drift_before_publish,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "changed before conditional publication",
                    ),
                ):
                    MODULE._complete_scheduler_attempt(
                        case_home,
                        attempt=attempt,
                        success=True,
                        failure_reason=None,
                        failure_code=None,
                        mode="public",
                        repo="owner/public-sync",
                        base_repo="owner/public-sync",
                        owner=MODULE.PUBLIC_OWNER,
                    )

                self.assertEqual(
                    (
                        status_path.stat().st_dev,
                        status_path.stat().st_ino,
                    ),
                    original_identity,
                )
                if drift == "content":
                    self.assertEqual(
                        status_path.read_bytes(),
                        b"foreign same-inode state\n",
                    )
                else:
                    self.assertEqual(
                        stat.S_IMODE(status_path.stat().st_mode),
                        0o640,
                    )

    def test_scheduler_runtime_cas_preserves_absent_to_appeared_state(
        self,
    ) -> None:
        status_path = MODULE._scheduler_status_path(self.home)
        appeared = b"foreign concurrently-created scheduler state\n"
        real_publish = MODULE._atomic_write_scheduler_config

        def appear_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.write_bytes(appeared)
            status_path.chmod(0o600)
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=appear_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._begin_scheduler_attempt(
                self.home,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), appeared)

    def test_scheduler_runtime_cas_allows_mtime_only_transition(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        real_publish = MODULE._atomic_write_scheduler_config

        def touch_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            metadata = status_path.stat()
            os.utime(
                status_path,
                ns=(
                    metadata.st_atime_ns,
                    metadata.st_mtime_ns + 1_000_000,
                ),
            )
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_write_scheduler_config",
            side_effect=touch_before_publish,
        ):
            completed = MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertTrue(completed)
        state = MODULE._read_scheduler_runtime_state(self.home)
        assert state is not None
        self.assertTrue(state["success"])
        self.assertEqual(state["last_attempt"], attempt)
        self.assertFalse(
            MODULE._scheduler_runtime_publication_marker_path(status_path).exists()
        )

    def test_scheduler_runtime_cas_rejects_parent_rotation_with_same_file_inode(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original_payload = status_path.read_bytes()
        original_identity = (
            status_path.stat().st_dev,
            status_path.stat().st_ino,
        )
        rotated_parent = status_path.parent.with_name(
            status_path.parent.name + "-rotated"
        )
        real_publish = MODULE._atomic_write_scheduler_config

        def rotate_parent_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.parent.rename(rotated_parent)
            status_path.parent.mkdir(mode=0o700)
            os.link(
                rotated_parent / status_path.name,
                status_path,
                follow_symlinks=False,
            )
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=rotate_parent_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), original_payload)
        self.assertEqual(
            (status_path.stat().st_dev, status_path.stat().st_ino),
            original_identity,
        )
        self.assertEqual(
            (
                (rotated_parent / status_path.name).stat().st_dev,
                (rotated_parent / status_path.name).stat().st_ino,
            ),
            original_identity,
        )
        self.assertEqual(
            list(status_path.parent.glob(f".{status_path.name}.personal-sync-write-*")),
            [],
        )

    def test_timestamp_recovery_does_not_accept_unsafe_runtime_file(self) -> None:
        status_path = MODULE._scheduler_status_path(self.home)
        status_path.parent.mkdir(parents=True)
        status_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "last_attempt": "not-a-timestamp",
                    "last_success": None,
                    "success": False,
                    "failure_reason": None,
                    "failure_code": None,
                    "release_trees": {},
                    "mode": "public",
                    "repo": "owner/public-sync",
                    "base_repo": "owner/public-sync",
                    "owner": MODULE.PUBLIC_OWNER,
                }
            ),
            encoding="utf-8",
        )
        status_path.chmod(0o644)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "scheduler runtime state is invalid",
        ):
            MODULE._read_scheduler_runtime_state(
                self.home,
                recover_timestamps=True,
            )

    def test_audit_and_doctor_detect_skill_issues_without_deletion(self) -> None:
        duplicate_one = self.write_skill("duplicate-one", "duplicate-name")
        duplicate_two = self.write_skill("duplicate-two", "duplicate-name")
        managed_drift = self.write_skill("managed-drift", "managed-name")
        cache_entry = self.home / "skills" / ".cache"
        cache_entry.mkdir()
        backup_entry = self.home / "skills" / "bug-triage-playbook.bak-20260312-145916"
        backup_entry.mkdir()
        system_backup_entry = (
            self.home / "skills" / ".system" / "legacy-system-skill.bak-20260723"
        )
        system_backup_entry.mkdir(parents=True)
        broken_link = self.home / "skills" / "broken-link"
        broken_link.symlink_to(self.root / "missing-skill", target_is_directory=True)
        record = MODULE.ManagedLinkRecord(
            source=PurePosixPath("personal_codex/skills/managed-drift"),
            target=PurePosixPath("skills/managed-drift"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            link_target="../personal-sync/releases/expected/managed-drift",
            release_sha=PUBLIC_SHA,
        )
        managed_state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: PUBLIC_SHA},
            links={record.target: record},
        )
        healthy_report = MODULE.SchedulerReport(
            platform="linux",
            installed=True,
            enabled=True,
            config_paths=(self.root / "scheduler.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            stable_runner=True,
            mode="public",
            base_repo="owner/public-sync",
            private_repo=None,
            last_attempt=None,
            recent_success=None,
            current_releases=(),
            failure_reason=None,
        )
        before = snapshot_tree(self.home / "skills")

        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            return_value=managed_state,
        ):
            audit_issues = MODULE.audit_active_skills(self.home)
        audit_codes = {issue.code for issue in audit_issues}
        self.assertTrue(
            {
                "unmanaged-skill",
                "broken-link",
                "duplicate-skill-name",
                "cache-or-backup",
                "generated-drift",
            }.issubset(audit_codes)
        )
        self.assertEqual(
            {issue.path for issue in audit_issues if issue.code == "unmanaged-skill"},
            {duplicate_one, duplicate_two},
        )
        self.assertEqual(
            {
                issue.path
                for issue in audit_issues
                if issue.code == "duplicate-skill-name"
            },
            {duplicate_one, duplicate_two},
        )
        self.assertEqual(
            {issue.path for issue in audit_issues if issue.code == "cache-or-backup"},
            {cache_entry, backup_entry, system_backup_entry},
        )
        self.assertIn(
            ("broken-link", broken_link),
            {(issue.code, issue.path) for issue in audit_issues},
        )
        self.assertIn(
            ("generated-drift", managed_drift),
            {(issue.code, issue.path) for issue in audit_issues},
        )
        self.assertEqual(snapshot_tree(self.home / "skills"), before)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_load_managed_state",
                return_value=managed_state,
            ),
            mock.patch.object(
                MODULE,
                "scheduler_report",
                return_value=healthy_report,
            ),
            contextlib.redirect_stdout(output),
        ):
            report, doctor_issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        self.assertIs(report, healthy_report)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            {issue["code"] for issue in payload["issues"]},
            {issue.code for issue in doctor_issues},
        )
        self.assertTrue(
            {
                "unmanaged-skill",
                "broken-link",
                "duplicate-skill-name",
                "cache-or-backup",
                "generated-drift",
            }.issubset({issue.code for issue in doctor_issues})
        )
        self.assertEqual(snapshot_tree(self.home / "skills"), before)

    def test_macos_loader_rejects_program_override(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        payload = MODULE._launchd_plist(
            self.home,
            "owner/public-sync",
            19,
            runner,
        )
        payload["Program"] = "/tmp/attacker"
        paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported execution semantics",
        ):
            MODULE._load_macos_scheduler_config(paths)

    def test_linux_loader_rejects_extra_execution_semantics_and_dropins(
        self,
    ) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        service = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        ).replace(
            "ExecStart=",
            'ExecStartPre="/tmp/attacker"\nExecStart=',
        )
        paths.systemd_service.write_text(service, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(23),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported execution semantics",
        ):
            MODULE._load_linux_scheduler_config(paths)

        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        drop_in = paths.systemd_service.with_name(paths.systemd_service.name + ".d")
        drop_in.mkdir()
        (drop_in / "override.conf").write_text(
            "[Service]\nExecStartPre=/tmp/attacker\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "drop-ins are unsupported",
        ):
            MODULE._load_linux_scheduler_config(paths)

    def test_systemd_exec_arguments_preserve_literal_special_paths(self) -> None:
        runner = (
            self.home / "bin with spaces" / 'runner %h $RUNNER "quoted" \\ backslash'
        )
        expected = MODULE._scheduler_install_args(
            runner,
            "owner/public-sync",
            self.home,
        )
        service = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        )
        exec_start = next(
            line.partition("=")[2]
            for line in service.splitlines()
            if line.startswith("ExecStart=")
        )

        self.assertIn("%%h", exec_start)
        self.assertIn("$$RUNNER", exec_start)
        self.assertIn('\\"quoted\\"', exec_start)
        self.assertIn("\\\\ backslash", exec_start)
        self.assertEqual(
            MODULE._parse_systemd_exec_arguments(exec_start),
            expected,
        )

    def test_linux_loader_decodes_exact_systemd_runtime_arguments(self) -> None:
        runner = (
            self.home / "bin with spaces" / 'runner %h $RUNNER "quoted" \\ backslash'
        )
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        service = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        )
        paths.systemd_service.write_text(service, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(23),
            encoding="utf-8",
        )

        config = MODULE._load_linux_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.repo, "owner/public-sync")

    def test_systemd_exec_parser_rejects_expansion_and_shell_forms(self) -> None:
        commands = {
            "specifier": '"/absolute/runner%h" "run-scheduled"',
            "variable": '"/absolute/$RUNNER" "run-scheduled"',
            "single-quote": "'/absolute/runner' 'run-scheduled'",
            "backslash-escape": '"/absolute/runner\\sname" "run-scheduled"',
        }
        for name, command in commands.items():
            with self.subTest(name=name), self.assertRaises(MODULE.SyncError):
                MODULE._parse_systemd_exec_arguments(command)

    def test_systemd_arguments_reject_controls_and_invalid_utf8(self) -> None:
        rejected = {
            "newline": "\n",
            "nul": "\0",
            "tab": "\t",
            "delete": "\x7f",
            "c1-control": "\x85",
            "surrogate": "\udcff",
        }
        for name, character in rejected.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "valid UTF-8|control characters",
                ),
            ):
                MODULE._systemd_quote(f"/absolute/runner{character}unsafe")

    def test_linux_install_rejects_control_path_before_transaction(self) -> None:
        runner = self.home / "bin" / "runner\nunsafe"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        with (
            mock.patch.object(MODULE, "_install_scheduler_transaction") as install,
            self.assertRaisesRegex(MODULE.SyncError, "control characters"),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                23,
                "linux",
                str(runner),
                dry_run=False,
                enable=False,
            )
        install.assert_not_called()

    def test_linux_interval_parser_is_bounded(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        timer = MODULE._systemd_timer(1).replace(
            "OnUnitActiveSec=1min",
            f"OnUnitActiveSec={'9' * 4301}min",
        )
        paths.systemd_timer.write_text(timer, encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "interval must use whole minutes",
        ):
            MODULE._load_linux_scheduler_config(paths)

    def test_install_rejects_invalid_inputs_before_writing(self) -> None:
        self.write_runner()
        with (
            mock.patch.object(MODULE, "_write_text") as write_text,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "owner/repo",
            ),
        ):
            MODULE.install_scheduler(
                self.home,
                "not-a-repository",
                30,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )
        write_text.assert_not_called()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "must not exceed",
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/repository",
                MODULE.MAX_SCHEDULER_INTERVAL_MINUTES + 1,
                "linux",
                None,
                dry_run=True,
                enable=False,
            )

    def test_runner_validation_rejects_relative_and_directory_paths(self) -> None:
        with self.assertRaisesRegex(MODULE.SyncError, "absolute path"):
            MODULE._validate_scheduler_runner(
                Path("relative-runner"),
                dry_run=True,
            )
        runner_directory = self.home / "bin" / "runner-directory"
        runner_directory.mkdir(parents=True)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "regular executable",
        ):
            MODULE._validate_scheduler_runner(
                runner_directory,
                dry_run=False,
            )

    def test_stable_runner_requires_managed_symlink_claim(self) -> None:
        plain_runner = self.write_runner()
        config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "timer",),
            interval_minutes=60,
            runner=plain_runner,
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertFalse(MODULE._stable_scheduler_runner_matches(self.home, config))

        plain_runner.unlink()
        managed_runner = (
            self.home
            / "personal-sync"
            / "releases"
            / PUBLIC_SHA
            / "scripts"
            / "codex_personal_sync.py"
        )
        managed_runner.parent.mkdir(parents=True)
        managed_runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_runner.chmod(0o755)
        link_target = os.path.relpath(managed_runner, plain_runner.parent)
        plain_runner.symlink_to(link_target)
        record = MODULE.ManagedLinkRecord(
            source=PurePosixPath("scripts/codex_personal_sync.py"),
            target=PurePosixPath("bin/codex-personal-sync"),
            kind="file",
            owner=MODULE.PUBLIC_OWNER,
            link_target=link_target,
            release_sha=PUBLIC_SHA,
        )
        state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: PUBLIC_SHA},
            links={record.target: record},
        )
        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            return_value=state,
        ):
            self.assertTrue(MODULE._stable_scheduler_runner_matches(self.home, config))

        alternate = self.home / "alternate-runner"
        alternate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        alternate.chmod(0o755)

        def replace_link_then_return_state(
            _home: Path,
        ) -> MODULE.ManagedState:
            plain_runner.unlink()
            plain_runner.symlink_to(os.path.relpath(alternate, plain_runner.parent))
            return state

        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            side_effect=replace_link_then_return_state,
        ):
            self.assertFalse(MODULE._stable_scheduler_runner_matches(self.home, config))

    def test_runtime_target_mismatch_is_unhealthy_and_not_carried_forward(
        self,
    ) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/current",
                runner,
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(31),
            encoding="utf-8",
        )
        stale_runtime = {
            "version": 1,
            "last_attempt": "2026-07-23T08:00:00+00:00",
            "last_success": "2026-07-23T08:00:00+00:00",
            "success": False,
            "failure_reason": "old target failed",
            "mode": "public",
            "repo": "owner/old",
            "base_repo": "owner/old",
            "owner": MODULE.PUBLIC_OWNER,
        }
        with mock.patch.object(
            MODULE,
            "_read_scheduler_runtime_state",
            return_value=stale_runtime,
        ):
            report = MODULE.scheduler_report(self.home, "linux")
        self.assertEqual(
            report.failure_reason,
            "scheduler runtime state belongs to a different configured target",
        )
        self.assertEqual(report.failure_code, "scheduler-target-mismatch")
        self.assertIsNone(report.last_attempt)
        self.assertIsNone(report.recent_success)
        next_payload = MODULE._scheduler_runtime_payload(
            previous=stale_runtime,
            attempt="2026-07-23T09:00:00+00:00",
            success=False,
            failure_reason="failed",
            mode="public",
            repo="owner/current",
            base_repo="owner/current",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertIsNone(next_payload["last_success"])

    def test_linux_pair_transaction_recovers_crash_and_preserves_interval(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/old",
            17,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        real_write = MODULE._write_text_with_activation_binding
        injected = False

        def fail_before_timer(
            path: Path,
            content: str,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot,
            description: str,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.SchedulerActivationBinding,
        ]:
            nonlocal injected
            if path == paths.systemd_timer and not injected:
                injected = True
                raise MODULE.SyncError("injected pair crash")
            return real_write(
                path,
                content,
                expected_snapshot=expected_snapshot,
                description=description,
            )

        with (
            mock.patch.object(
                MODULE,
                "_write_text_with_activation_binding",
                side_effect=fail_before_timer,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "pair crash"),
        ):
            self.install_scheduler_quietly(
                "owner/new",
                None,
                "linux",
            )

        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())
        self.install_scheduler_quietly(
            "owner/new",
            None,
            "linux",
        )
        self.assertFalse(MODULE._scheduler_pair_transaction_path(paths).exists())
        config = MODULE._load_linux_scheduler_config(paths)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.repo, "owner/new")
        self.assertEqual(config.interval_minutes, 17)

    def test_concurrent_linux_installs_serialize_recovery_through_cleanup(
        self,
    ) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        first_publication_paused = threading.Event()
        release_first_publication = threading.Event()
        second_install_started = threading.Event()
        second_recovery_entered = threading.Event()
        errors: dict[str, BaseException] = {}
        real_write = MODULE._write_text_with_activation_binding
        real_recover = MODULE._recover_scheduler_pair_transaction

        def pause_first_publication(
            path: Path,
            content: str,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot,
            description: str,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.SchedulerActivationBinding,
        ]:
            if (
                threading.current_thread().name == "first-scheduler-install"
                and path == paths.systemd_service
            ):
                first_publication_paused.set()
                if not release_first_publication.wait(5):
                    raise AssertionError("first scheduler publication was not released")
            return real_write(
                path,
                content,
                expected_snapshot=expected_snapshot,
                description=description,
            )

        def observe_recovery(
            selected_paths: MODULE.SchedulerPaths,
            *,
            dry_run: bool,
        ) -> bool:
            if threading.current_thread().name == "second-scheduler-install":
                second_recovery_entered.set()
            return real_recover(selected_paths, dry_run=dry_run)

        def install(name: str, repo: str, interval: int) -> None:
            if name == "second":
                second_install_started.set()
            try:
                MODULE.install_scheduler(
                    self.home,
                    repo,
                    interval,
                    "linux",
                    None,
                    dry_run=False,
                    enable=False,
                )
            except BaseException as error:
                errors[name] = error

        first = threading.Thread(
            target=install,
            args=("first", "owner/first", 17),
            name="first-scheduler-install",
            daemon=True,
        )
        second = threading.Thread(
            target=install,
            args=("second", "owner/second", 29),
            name="second-scheduler-install",
            daemon=True,
        )
        with (
            mock.patch.object(
                MODULE,
                "_write_text_with_activation_binding",
                side_effect=pause_first_publication,
            ),
            mock.patch.object(
                MODULE,
                "_recover_scheduler_pair_transaction",
                side_effect=observe_recovery,
            ),
        ):
            try:
                first.start()
                self.assertTrue(first_publication_paused.wait(5))
                self.assertTrue(
                    MODULE._scheduler_pair_transaction_path(paths).is_file()
                )

                second.start()
                self.assertTrue(second_install_started.wait(5))
                self.assertFalse(second_recovery_entered.wait(0.2))
                self.assertTrue(
                    MODULE._scheduler_pair_transaction_path(paths).is_file()
                )
            finally:
                release_first_publication.set()
                if first.ident is not None:
                    first.join(5)
                if second.ident is not None:
                    second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, {})
        self.assertTrue(second_recovery_entered.is_set())
        self.assertFalse(MODULE._scheduler_pair_transaction_path(paths).exists())
        config = MODULE._load_linux_scheduler_config(paths)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.repo, "owner/second")
        self.assertEqual(config.interval_minutes, 29)

    def test_linux_pair_transaction_refuses_concurrent_editor_state(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "linux")
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        original_timer = paths.systemd_timer.read_bytes()
        real_write = MODULE._write_text_with_activation_binding
        injected = False

        def edit_before_conditional_write(
            path: Path,
            content: str,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot,
            description: str,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.SchedulerActivationBinding,
        ]:
            nonlocal injected
            if path == paths.systemd_service and not injected:
                injected = True
                path.write_text("user edit\n", encoding="utf-8")
                path.chmod(0o600)
            return real_write(
                path,
                content,
                expected_snapshot=expected_snapshot,
                description=description,
            )

        with (
            mock.patch.object(
                MODULE,
                "_write_text_with_activation_binding",
                side_effect=edit_before_conditional_write,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")

        self.assertEqual(
            paths.systemd_service.read_text(encoding="utf-8"),
            "user edit\n",
        )
        self.assertEqual(paths.systemd_timer.read_bytes(), original_timer)
        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "service changed during pending pair transaction",
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")
        self.assertEqual(
            paths.systemd_service.read_text(encoding="utf-8"),
            "user edit\n",
        )

    def test_systemd_pair_recovery_retains_marker_on_member_replacement(
        self,
    ) -> None:
        real_parse = MODULE._parse_scheduler_pair_transaction
        for target_kind in ("service", "timer"):
            with self.subTest(target=target_kind):
                case_user_home = self.root / f"pair-replacement-{target_kind}" / "home"
                case_home = case_user_home / ".codex"
                paths = self.write_pending_systemd_pair(
                    case_user_home,
                    case_home,
                )
                target = (
                    paths.systemd_service
                    if target_kind == "service"
                    else paths.systemd_timer
                )
                assert target is not None
                marker = MODULE._scheduler_pair_transaction_path(paths)
                replaced = False

                def parse_then_replace(
                    payload: bytes,
                    path: Path,
                ) -> tuple[
                    MODULE.ManagedStateFileSnapshot,
                    MODULE.ManagedStateFileSnapshot,
                    bytes,
                    bytes,
                ]:
                    nonlocal replaced
                    parsed = real_parse(payload, path)
                    replacement = target.with_name(target.name + ".replacement")
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                    os.replace(replacement, target)
                    replaced = True
                    return parsed

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_parse_scheduler_pair_transaction",
                        side_effect=parse_then_replace,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "recovery object group changed",
                    ),
                ):
                    MODULE._recover_scheduler_pair_transaction(
                        paths,
                        dry_run=False,
                    )

                self.assertTrue(replaced)
                self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_revalidates_absent_group_after_parent_sync(
        self,
    ) -> None:
        for mutation in ("during-fsync", "during-second-pass"):
            with self.subTest(mutation=mutation):
                case_user_home = self.root / f"pair-absent-{mutation}" / "home"
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths("linux", case_home)
                assert paths.systemd_service is not None
                assert paths.systemd_timer is not None
                paths.systemd_service.parent.mkdir(parents=True)
                marker = MODULE._scheduler_pair_transaction_path(paths)
                absent = MODULE.ManagedStateFileSnapshot(exists=False)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    marker_before = MODULE._scheduler_config_snapshot(
                        marker,
                        MODULE.MAX_SCHEDULER_PAIR_TRANSACTION_BYTES,
                    )
                    MODULE._atomic_write_scheduler_config(
                        marker,
                        MODULE._scheduler_pair_transaction_payload(
                            service_before=absent,
                            timer_before=absent,
                            service_after=b"future service\n",
                            timer_after=b"future timer\n",
                        ),
                        expected_snapshot=marker_before,
                    )

                real_fsync = MODULE.os.fsync
                real_member_check = MODULE._revalidate_systemd_pair_recovery_member
                armed = False
                injected = False
                timer_checks = 0

                def reappear_service() -> None:
                    nonlocal injected
                    paths.systemd_service.write_bytes(b"concurrent service\n")
                    paths.systemd_service.chmod(0o600)
                    injected = True

                def sync_then_arm_or_reappear(file_fd: int) -> None:
                    nonlocal armed
                    real_fsync(file_fd)
                    if mutation == "during-fsync":
                        reappear_service()
                    else:
                        armed = True

                def reappear_after_earlier_absence_check(
                    group: MODULE.SystemdPairRecoveryGroup,
                    member: MODULE.SystemdPairRecoveryMember,
                ) -> None:
                    nonlocal timer_checks
                    if (
                        mutation == "during-second-pass"
                        and armed
                        and member.path == paths.systemd_timer
                    ):
                        timer_checks += 1
                        if timer_checks == 2:
                            reappear_service()
                    real_member_check(group, member)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "fsync",
                        side_effect=sync_then_arm_or_reappear,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_revalidate_systemd_pair_recovery_member",
                        side_effect=reappear_after_earlier_absence_check,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "recovery object group changed",
                    ) as raised,
                ):
                    MODULE._recover_scheduler_pair_transaction(
                        paths,
                        dry_run=False,
                    )

                self.assertTrue(injected)
                self.assertIn(str(paths.systemd_service), str(raised.exception))
                self.assertIn(
                    "after parent sync before transaction marker commit",
                    str(raised.exception),
                )
                self.assertTrue(paths.systemd_service.is_file())
                self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_rechecks_earlier_member_after_later_member(
        self,
    ) -> None:
        case_user_home = self.root / "pair-later-member-race" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        marker = MODULE._scheduler_pair_transaction_path(paths)
        real_parse = MODULE._parse_scheduler_pair_transaction
        real_matches = MODULE._scheduler_recovery_binding_matches
        armed = False
        replaced = False

        def parse_and_arm(
            payload: bytes,
            path: Path,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.ManagedStateFileSnapshot,
            bytes,
            bytes,
        ]:
            nonlocal armed
            parsed = real_parse(payload, path)
            armed = True
            return parsed

        def replace_service_while_timer_is_checked(
            home: Path,
            file_fd: int,
            path: Path,
            parent_fd: int,
            expected: MODULE.ManagedStateFileSnapshot,
        ) -> bool:
            nonlocal replaced
            if armed and not replaced and path == paths.systemd_timer:
                replacement = paths.systemd_service.with_name(
                    paths.systemd_service.name + ".replacement"
                )
                replacement.write_bytes(paths.systemd_service.read_bytes())
                replacement.chmod(stat.S_IMODE(paths.systemd_service.stat().st_mode))
                os.replace(replacement, paths.systemd_service)
                replaced = True
            return real_matches(
                home,
                file_fd,
                path,
                parent_fd,
                expected,
            )

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_parse_scheduler_pair_transaction",
                side_effect=parse_and_arm,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_recovery_binding_matches",
                side_effect=replace_service_while_timer_is_checked,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "recovery object group changed",
            ),
        ):
            MODULE._recover_scheduler_pair_transaction(
                paths,
                dry_run=False,
            )

        self.assertTrue(replaced)
        self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_closes_partial_bindings(self) -> None:
        case_user_home = self.root / "pair-partial-bind" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        real_bind = MODULE._bind_systemd_pair_recovery_member
        bound_fds: list[int] = []

        def bind_marker_then_fail(
            home: Path,
            path: Path,
            parent_fd: int,
            *,
            maximum_bytes: int = 1024 * 1024,
        ) -> MODULE.SystemdPairRecoveryMember:
            if path == paths.systemd_service:
                raise MODULE.SyncError("simulated service binding failure")
            member = real_bind(
                home,
                path,
                parent_fd,
                maximum_bytes=maximum_bytes,
            )
            if member.file_fd >= 0:
                bound_fds.append(member.file_fd)
            return member

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_bind_systemd_pair_recovery_member",
                side_effect=bind_marker_then_fail,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "simulated service binding failure",
            ),
        ):
            with MODULE._retain_systemd_pair_recovery_group(paths):
                self.fail("partial recovery binding unexpectedly succeeded")

        self.assertTrue(bound_fds)
        for file_fd in bound_fds:
            with self.assertRaises(OSError):
                os.fstat(file_fd)

    def test_systemd_pair_recovery_retains_marker_across_parent_aba(
        self,
    ) -> None:
        case_user_home = self.root / "pair-parent-aba" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        unit_parent = paths.systemd_service.parent
        displaced_parent = unit_parent.with_name(unit_parent.name + ".displaced")
        transient_parent = unit_parent.with_name(unit_parent.name + ".transient")
        marker = MODULE._scheduler_pair_transaction_path(paths)
        real_parse = MODULE._parse_scheduler_pair_transaction
        rotated = False

        def parse_then_rotate_parent(
            payload: bytes,
            path: Path,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.ManagedStateFileSnapshot,
            bytes,
            bytes,
        ]:
            nonlocal rotated
            parsed = real_parse(payload, path)
            unit_parent.rename(displaced_parent)
            unit_parent.mkdir()
            for source in displaced_parent.iterdir():
                if not source.is_file():
                    continue
                destination = unit_parent / source.name
                destination.write_bytes(source.read_bytes())
                destination.chmod(stat.S_IMODE(source.stat().st_mode))
            rotated = True
            return parsed

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_parse_scheduler_pair_transaction",
                side_effect=parse_then_rotate_parent,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "recovery object group changed",
            ),
        ):
            MODULE._recover_scheduler_pair_transaction(
                paths,
                dry_run=False,
            )

        self.assertTrue(rotated)
        self.assertTrue((displaced_parent / marker.name).is_file())
        unit_parent.rename(transient_parent)
        displaced_parent.rename(unit_parent)
        self.assertTrue(marker.is_file())
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(
                MODULE._recover_scheduler_pair_transaction(
                    paths,
                    dry_run=False,
                )
            )
        self.assertFalse(marker.exists())

    def test_macos_install_binds_semantically_audited_snapshot(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "macos")
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        real_audit = MODULE._audit_scheduler_config
        replacement = b"user edited launchd config\n"

        def audit_then_edit(
            selected_paths: MODULE.SchedulerPaths,
        ) -> MODULE.SchedulerConfigAudit:
            audit = real_audit(selected_paths)
            paths.launchd_plist.write_bytes(replacement)
            paths.launchd_plist.chmod(0o600)
            return audit

        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                side_effect=audit_then_edit,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "macos")

        self.assertEqual(paths.launchd_plist.read_bytes(), replacement)

    def test_macos_install_revalidates_bound_plist_at_every_native_boundary(
        self,
    ) -> None:
        action_names = (
            "legacy-bootout",
            "legacy-disable",
            "current-bootout",
            "bootstrap",
            "enable",
        )
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        for action_index, action_name in enumerate(action_names):
            for mutation, expected_error in mutations:
                with self.subTest(action=action_name, mutation=mutation):
                    case_user_home = self.root / f"{action_index}-{mutation}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    paths = MODULE.SchedulerPaths(
                        platform="macos",
                        launchd_plist=(
                            case_user_home
                            / "Library"
                            / "LaunchAgents"
                            / f"{MODULE.LAUNCHD_LABEL}.plist"
                        ),
                    )
                    assert paths.launchd_plist is not None
                    native_calls = 0
                    force_unreadable = False
                    real_read = MODULE._read_managed_state_bytes

                    def mutate_during_native(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal force_unreadable, native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        plist = paths.launchd_plist
                        if mutation == "replacement":
                            replacement = plist.with_name(plist.name + ".replacement")
                            replacement.write_bytes(plist.read_bytes())
                            replacement.chmod(0o600)
                            os.replace(replacement, plist)
                        elif mutation == "content":
                            payload = bytearray(plist.read_bytes())
                            payload[len(payload) // 2] ^= 1
                            plist.write_bytes(payload)
                            plist.chmod(0o600)
                        elif mutation == "mode":
                            plist.chmod(0o644)
                        elif mutation == "parent":
                            old_parent = plist.parent.with_name(
                                plist.parent.name + ".replaced"
                            )
                            plist.parent.rename(old_parent)
                            plist.parent.mkdir()
                            replacement = plist.parent / plist.name
                            replacement.write_bytes(
                                (old_parent / plist.name).read_bytes()
                            )
                            replacement.chmod(0o600)
                        elif mutation == "missing":
                            plist.unlink()
                        else:
                            force_unreadable = True

                    def fail_bound_read(
                        file_fd: int,
                        path: Path,
                        maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
                    ) -> bytes:
                        if force_unreadable and path == paths.launchd_plist:
                            raise MODULE.SyncError("injected read failure")
                        return real_read(file_fd, path, maximum_bytes)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_during_native,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_read_managed_state_bytes",
                            side_effect=fail_bound_read,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    self.assertNotIn(
                        "installed macOS launchd scheduler",
                        output.getvalue(),
                    )

    def test_macos_install_prebinds_every_legacy_before_cleanup(self) -> None:
        case_user_home = self.root / "install-all-legacy-bindings" / "home"
        case_home = case_user_home / ".codex"
        runner = case_home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        labels = (
            "com.joeyteng.codex-personal-sync.one",
            "com.joeyteng.codex-personal-sync.two",
        )
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "LEGACY_LAUNCHD_LABELS",
                labels,
            ),
        ):
            paths = MODULE._scheduler_paths("macos", case_home)
            second_legacy = MODULE._legacy_launchd_plist(paths, labels[1])
        second_legacy.parent.mkdir(parents=True)
        second_legacy.write_bytes(b"original second legacy\n")
        second_legacy.chmod(0o600)
        replacement = b"replacement second legacy\n"
        native_calls = 0

        def replace_second_legacy_during_first_cleanup_action(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            nonlocal native_calls
            native_calls += 1
            candidate = second_legacy.with_name(second_legacy.name + ".replacement")
            candidate.write_bytes(replacement)
            candidate.chmod(0o600)
            os.replace(candidate, second_legacy)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "LEGACY_LAUNCHD_LABELS",
                labels,
            ),
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=replace_second_legacy_during_first_cleanup_action,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "object identity changed",
            ),
        ):
            MODULE.install_scheduler(
                case_home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(native_calls, 1)
        self.assertEqual(second_legacy.read_bytes(), replacement)
        assert paths.launchd_plist is not None
        self.assertTrue(paths.launchd_plist.exists())
        self.assertNotIn(
            "installed macOS launchd scheduler",
            output.getvalue(),
        )

    def test_macos_install_legacy_cleanup_native_failures_stop_migration(
        self,
    ) -> None:
        success = subprocess.CompletedProcess(
            ["scheduler-action"],
            0,
            "",
            "",
        )
        failures: tuple[
            tuple[str, int, BaseException | subprocess.CompletedProcess[str], str],
            ...,
        ] = (
            (
                "timeout",
                0,
                MODULE.SyncError(
                    "scheduler native command exceeded its monotonic deadline",
                    code="scheduler-timeout",
                ),
                "failed to run launchctl bootout",
            ),
            (
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["launchctl", "bootout"],
                    1,
                    "",
                    "Operation not permitted",
                ),
                "Operation not permitted",
            ),
            (
                "unknown",
                1,
                subprocess.CompletedProcess(
                    ["launchctl", "disable"],
                    1,
                    "",
                    "Input/output error",
                ),
                "Input/output error",
            ),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for failure_kind, failure_call, failure, expected_error in failures:
            with self.subTest(failure=failure_kind):
                case_user_home = self.root / f"install-legacy-{failure_kind}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths("macos", case_home)
                assert paths.launchd_plist is not None
                legacy = MODULE._legacy_launchd_plist(paths, label)
                legacy.parent.mkdir(parents=True)
                legacy_payload = b"legacy scheduler config\n"
                legacy.write_bytes(legacy_payload)
                legacy.chmod(0o600)
                results: list[BaseException | subprocess.CompletedProcess[str]] = [
                    success
                ] * failure_call + [failure]
                native_calls: list[list[str]] = []

                def run_native(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    native_calls.append(args)
                    result = results.pop(0)
                    if isinstance(result, BaseException):
                        raise result
                    return result

                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=run_native,
                    ),
                    contextlib.redirect_stdout(output),
                    self.assertRaisesRegex(MODULE.SyncError, expected_error),
                ):
                    MODULE.install_scheduler(
                        case_home,
                        "owner/public-sync",
                        17,
                        "macos",
                        None,
                        dry_run=False,
                        enable=True,
                    )

                self.assertEqual(results, [])
                self.assertEqual(len(native_calls), failure_call + 1)
                self.assertEqual(native_calls[0][1], "bootout")
                if failure_call:
                    self.assertEqual(native_calls[1][1], "disable")
                self.assertFalse(
                    any(
                        len(args) > 1 and args[1] in {"bootstrap", "enable"}
                        for args in native_calls
                    )
                )
                self.assertEqual(legacy.read_bytes(), legacy_payload)
                self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o600)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    config = MODULE._load_macos_scheduler_config(paths)
                self.assertIsNotNone(config)
                assert config is not None
                self.assertEqual(config.repo, "owner/public-sync")
                self.assertNotIn(
                    "installed macOS launchd scheduler",
                    output.getvalue(),
                )

    def test_macos_install_legacy_cleanup_accepts_precise_absence(
        self,
    ) -> None:
        case_user_home = self.root / "install-legacy-already-absent" / "home"
        case_home = case_user_home / ".codex"
        runner = case_home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        with mock.patch.object(
            MODULE.Path,
            "home",
            return_value=case_user_home,
        ):
            paths = MODULE._scheduler_paths("macos", case_home)
        assert paths.launchd_plist is not None
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        legacy = MODULE._legacy_launchd_plist(paths, label)
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy scheduler config\n")
        legacy.chmod(0o600)
        results = [
            subprocess.CompletedProcess(
                ["launchctl", "bootout"],
                1,
                "",
                "Boot-out failed: 3: No such process",
            ),
            subprocess.CompletedProcess(
                ["launchctl", "disable"],
                1,
                "",
                "Could not find specified service",
            ),
            *(
                subprocess.CompletedProcess(
                    ["scheduler-action"],
                    0,
                    "",
                    "",
                )
                for _ in range(len(MODULE.LEGACY_LAUNCHD_LABELS) * 4 + 4)
            ),
        ]
        native_calls: list[list[str]] = []

        def run_native(
            args: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            native_calls.append(args)
            return results.pop(0)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=run_native,
            ),
            contextlib.redirect_stdout(output),
        ):
            MODULE.install_scheduler(
                case_home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(results, [])
        self.assertEqual(
            [args[1] for args in native_calls],
            ["bootout", "disable", "bootout", "disable"]
            * len(MODULE.LEGACY_LAUNCHD_LABELS)
            + ["bootout", "disable", "bootout", "enable", "bootstrap", "enable"],
        )
        self.assertFalse(legacy.exists())
        with mock.patch.object(
            MODULE.Path,
            "home",
            return_value=case_user_home,
        ):
            config = MODULE._load_macos_scheduler_config(paths)
        self.assertIsNotNone(config)
        self.assertEqual(
            output.getvalue().count("ignored already-absent scheduler command"),
            2,
        )
        self.assertIn("installed macOS launchd scheduler", output.getvalue())

    def test_macos_install_retains_legacy_absence_through_current_actions(
        self,
    ) -> None:
        legacy_action_count = len(MODULE.LEGACY_LAUNCHD_LABELS) * 4
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        case_index = 0
        for initial_legacy_exists in (False, True):
            for current_action_offset in range(6):
                case_index += 1
                with self.subTest(
                    initial_legacy_exists=initial_legacy_exists,
                    current_action=current_action_offset,
                ):
                    case_user_home = (
                        self.root / f"install-legacy-retained-{case_index}" / "home"
                    )
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths("macos", case_home)
                    assert paths.launchd_plist is not None
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    legacy.parent.mkdir(parents=True)
                    if initial_legacy_exists:
                        legacy.write_bytes(b"original legacy scheduler\n")
                        legacy.chmod(0o600)
                    payload = (
                        f"new legacy {initial_legacy_exists} {current_action_offset}\n"
                    ).encode("utf-8")
                    mutation_action = legacy_action_count + current_action_offset
                    native_calls = 0

                    def reappear_during_current_action(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != mutation_action:
                            return
                        legacy.write_bytes(payload)
                        legacy.chmod(0o600)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=reappear_during_current_action,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            (
                                "reappeared after conditional removal"
                                if initial_legacy_exists
                                else "appeared after initial absence"
                            ),
                        ),
                    ):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertEqual(native_calls, mutation_action + 1)
                    self.assertEqual(legacy.read_bytes(), payload)
                    self.assertTrue(paths.launchd_plist.exists())
                    self.assertNotIn(
                        "installed macOS launchd scheduler",
                        output.getvalue(),
                    )

    def test_macos_install_allows_mtime_only_churn_at_native_boundaries(
        self,
    ) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        native_calls = 0

        def touch_during_native(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            nonlocal native_calls
            native_calls += 1
            metadata = paths.launchd_plist.stat()
            os.utime(
                paths.launchd_plist,
                ns=(
                    metadata.st_atime_ns,
                    metadata.st_mtime_ns + 1_000_000,
                ),
            )

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=touch_during_native,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            native_calls,
            len(MODULE.LEGACY_LAUNCHD_LABELS) * 4 + 6,
        )
        self.assertIsNotNone(MODULE._load_macos_scheduler_config(paths))

    def test_legacy_launchd_cleanup_binds_file_across_native_actions(
        self,
    ) -> None:
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for mutation, expected_error in mutations:
            with self.subTest(mutation=mutation):
                case_user_home = self.root / f"legacy-{mutation}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                paths = MODULE.SchedulerPaths(
                    platform="macos",
                    launchd_plist=(
                        case_user_home
                        / "Library"
                        / "LaunchAgents"
                        / f"{MODULE.LAUNCHD_LABEL}.plist"
                    ),
                )
                legacy = MODULE._legacy_launchd_plist(paths, label)
                legacy.parent.mkdir(parents=True)
                original = b"legacy launchd config\n"
                replacement = b"user replacement config\n"
                legacy.write_bytes(original)
                legacy.chmod(0o600)
                force_unreadable = False
                real_read = MODULE._read_managed_state_bytes

                def mutate_legacy(
                    _args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool = False,
                ) -> None:
                    del dry_run, allow_fail
                    nonlocal force_unreadable
                    if mutation == "replacement":
                        candidate = legacy.with_name(legacy.name + ".replacement")
                        candidate.write_bytes(replacement)
                        candidate.chmod(0o600)
                        os.replace(candidate, legacy)
                    elif mutation == "content":
                        legacy.write_bytes(replacement)
                        legacy.chmod(0o600)
                    elif mutation == "mode":
                        legacy.chmod(0o644)
                    elif mutation == "parent":
                        displaced_parent = legacy.parent.with_name(
                            legacy.parent.name + ".displaced"
                        )
                        legacy.parent.rename(displaced_parent)
                        legacy.parent.mkdir()
                        candidate = legacy.parent / legacy.name
                        candidate.write_bytes(replacement)
                        candidate.chmod(0o600)
                    elif mutation == "missing":
                        legacy.unlink()
                    else:
                        force_unreadable = True

                def fail_legacy_read(
                    file_fd: int,
                    path: Path,
                    maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
                ) -> bytes:
                    if force_unreadable and path == legacy:
                        raise MODULE.SyncError("injected legacy read failure")
                    return real_read(file_fd, path, maximum_bytes)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=mutate_legacy,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_managed_state_bytes",
                        side_effect=fail_legacy_read,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(MODULE.SyncError, expected_error),
                ):
                    MODULE.install_scheduler(
                        case_home,
                        "owner/public-sync",
                        17,
                        "macos",
                        None,
                        dry_run=False,
                        enable=True,
                    )

                if mutation in {"replacement", "content"}:
                    self.assertEqual(legacy.read_bytes(), replacement)
                elif mutation == "mode":
                    self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o644)
                elif mutation == "parent":
                    displaced = legacy.parent.with_name(
                        legacy.parent.name + ".displaced"
                    )
                    self.assertEqual(
                        (displaced / legacy.name).read_bytes(),
                        original,
                    )
                    self.assertEqual(legacy.read_bytes(), replacement)
                elif mutation == "missing":
                    self.assertFalse(legacy.exists())
                else:
                    self.assertEqual(legacy.read_bytes(), original)

    def test_legacy_launchd_cleanup_allows_mtime_only_churn(self) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        legacy = MODULE._legacy_launchd_plist(paths, label)
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy launchd config\n")
        legacy.chmod(0o600)

        def touch_legacy(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            if legacy.exists():
                metadata = legacy.stat()
                os.utime(
                    legacy,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000,
                    ),
                )

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=touch_legacy,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertFalse(legacy.exists())

    def test_absent_legacy_launchd_cleanup_retains_parent_and_absence(self) -> None:
        mutations = (
            ("appearance", "appeared after initial absence"),
            ("parent", "parent chain changed"),
            ("unreadable", "parent descriptor became unreadable"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for action_index in range(2):
            for mutation, expected_error in mutations:
                with self.subTest(action=action_index, mutation=mutation):
                    case_user_home = (
                        self.root / f"legacy-absent-{action_index}-{mutation}" / "home"
                    )
                    paths = MODULE.SchedulerPaths(
                        platform="macos",
                        launchd_plist=(
                            case_user_home
                            / "Library"
                            / "LaunchAgents"
                            / f"{MODULE.LAUNCHD_LABEL}.plist"
                        ),
                    )
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    legacy.parent.mkdir(parents=True)
                    parent_identity = (
                        legacy.parent.stat().st_dev,
                        legacy.parent.stat().st_ino,
                    )
                    real_directory_identity = MODULE._directory_identity
                    native_calls = 0
                    force_unreadable = False

                    def mutate_absence(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal force_unreadable, native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        if mutation == "appearance":
                            legacy.write_bytes(b"new user scheduler config\n")
                            legacy.chmod(0o600)
                        elif mutation == "parent":
                            displaced = legacy.parent.with_name(
                                legacy.parent.name + ".displaced"
                            )
                            legacy.parent.rename(displaced)
                            legacy.parent.mkdir()
                        else:
                            force_unreadable = True

                    def directory_identity(
                        directory_fd: int,
                    ) -> tuple[int, int]:
                        identity = real_directory_identity(directory_fd)
                        if force_unreadable and identity == parent_identity:
                            raise OSError("injected parent read failure")
                        return identity

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_absence,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_directory_identity",
                            side_effect=directory_identity,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE._cleanup_legacy_launchd_schedulers(
                            paths,
                            dry_run=False,
                            disable=True,
                            remove=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    if mutation == "appearance":
                        self.assertEqual(
                            legacy.read_bytes(),
                            b"new user scheduler config\n",
                        )

    def test_uninstall_prebinds_legacy_before_current_launchd_actions(self) -> None:
        cases = (
            ("appearance", "appeared after initial absence"),
            ("replacement", "object identity changed"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        case_index = 0
        for action_index in range(2):
            for mutation, expected_error in cases:
                case_index += 1
                with self.subTest(action=action_index, mutation=mutation):
                    case_user_home = self.root / f"legacy-current-{case_index}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                "macos",
                                None,
                                dry_run=False,
                                enable=False,
                            )
                        paths = MODULE._scheduler_paths("macos", case_home)
                    assert paths.launchd_plist is not None
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    if mutation == "replacement":
                        legacy.write_bytes(b"original legacy config\n")
                        legacy.chmod(0o600)
                    replacement_payload = (
                        f"new user config {action_index} {mutation}\n".encode("utf-8")
                    )
                    native_calls = 0

                    def mutate_legacy_during_current_action(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        if mutation == "appearance":
                            legacy.write_bytes(replacement_payload)
                            legacy.chmod(0o600)
                            return
                        candidate = legacy.with_name(legacy.name + ".replacement")
                        candidate.write_bytes(replacement_payload)
                        candidate.chmod(0o600)
                        os.replace(candidate, legacy)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_legacy_during_current_action,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            "macos",
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    self.assertEqual(legacy.read_bytes(), replacement_payload)
                    self.assertTrue(paths.launchd_plist.exists())
                    self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_revalidates_legacy_after_current_removal(
        self,
    ) -> None:
        for initial_legacy_exists in (False, True):
            with self.subTest(initial_legacy_exists=initial_legacy_exists):
                suffix = "present" if initial_legacy_exists else "absent"
                case_user_home = self.root / f"legacy-current-removal-{suffix}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths("macos", case_home)
                assert paths.launchd_plist is not None
                legacy = MODULE._legacy_launchd_plist(
                    paths,
                    MODULE.LEGACY_LAUNCHD_LABELS[0],
                )
                if initial_legacy_exists:
                    legacy.write_bytes(b"original legacy config\n")
                    legacy.chmod(0o600)
                payload = (
                    f"concurrent {suffix} legacy config during current removal\n"
                ).encode("utf-8")
                isolate = MODULE._isolate_and_delete_pending_cleanup_file

                def appear_during_current_removal(
                    home: Path,
                    path: Path,
                    parent_fd: int,
                    expected: MODULE.ManagedStateFileSnapshot,
                    *,
                    label: str,
                ) -> None:
                    isolate(
                        home,
                        path,
                        parent_fd,
                        expected,
                        label=label,
                    )
                    if path != paths.launchd_plist:
                        self.assertEqual(path, legacy)
                        return
                    legacy.write_bytes(payload)
                    legacy.chmod(0o600)

                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                        side_effect=appear_during_current_removal,
                    ),
                    contextlib.redirect_stdout(output),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        (
                            "reappeared after conditional removal"
                            if initial_legacy_exists
                            else "appeared after initial absence"
                        ),
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        "macos",
                        dry_run=False,
                        disable=False,
                    )

                self.assertFalse(paths.launchd_plist.exists())
                self.assertEqual(legacy.read_bytes(), payload)
                self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_missing_config_parent_no_disable_is_idempotent_noop(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"missing-parent-{platform_name}-no-disable" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "missing-parent no-op acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "missing-parent no-op ran a native command"
                        ),
                    ) as native_command,
                    contextlib.redirect_stdout(output),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=False,
                    )

                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                self.assertFalse(MODULE._scheduler_config_parent(paths).exists())
                self.assertFalse(case_home.exists())
                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertIn("scheduler already absent", output.getvalue())

    def test_uninstall_missing_config_disables_orphan_daemon_by_identity(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            for precreate_parent in (False, True):
                with self.subTest(
                    platform=platform_name,
                    precreate_parent=precreate_parent,
                ):
                    case_user_home = (
                        self.root
                        / f"orphan-{platform_name}-{precreate_parent}"
                        / "home"
                    )
                    case_user_home.mkdir(parents=True)
                    case_home = case_user_home / ".codex"
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    if precreate_parent:
                        MODULE._scheduler_config_parent(paths).mkdir(parents=True)
                    native_calls: list[list[str]] = []

                    def capture_native(
                        args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool | str = False,
                    ) -> None:
                        self.assertFalse(dry_run)
                        del allow_fail
                        native_calls.append(args)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_scheduler_daemon_enabled",
                            side_effect=(
                                MODULE.SchedulerDaemonQuery("enabled"),
                                MODULE.SchedulerDaemonQuery(
                                    "disabled",
                                    "daemon is absent",
                                ),
                            ),
                        ) as daemon_query,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=capture_native,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(daemon_query.call_count, 2)
                    self.assertTrue(MODULE._scheduler_config_parent(paths).is_dir())
                    self.assertFalse(
                        MODULE._scheduler_uninstall_transaction_path(paths).exists()
                    )
                    if platform_name == "macos":
                        domain = f"gui/{os.getuid()}"
                        self.assertIn(
                            [
                                "launchctl",
                                "bootout",
                                f"{domain}/{MODULE.LAUNCHD_LABEL}",
                            ],
                            native_calls,
                        )
                        for label in MODULE.LEGACY_LAUNCHD_LABELS:
                            self.assertIn(
                                [
                                    "launchctl",
                                    "bootout",
                                    f"{domain}/{label}",
                                ],
                                native_calls,
                            )
                        assert paths.launchd_plist is not None
                        self.assertFalse(paths.launchd_plist.exists())
                    else:
                        self.assertIn(
                            [
                                "systemctl",
                                "--user",
                                "disable",
                                "--now",
                                f"{MODULE.SYSTEMD_UNIT}.timer",
                            ],
                            native_calls,
                        )
                        self.assertIn(
                            ["systemctl", "--user", "daemon-reload"],
                            native_calls,
                        )
                        assert paths.systemd_service is not None
                        assert paths.systemd_timer is not None
                        self.assertFalse(paths.systemd_service.exists())
                        self.assertFalse(paths.systemd_timer.exists())

    def test_macos_uninstall_cleans_managed_orphan_identity_matrix(self) -> None:
        legacy_label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        cases = (
            (
                "legacy-only",
                "disabled",
                "disabled",
                {
                    (
                        legacy_label,
                        MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
                    ): "enabled"
                },
                (legacy_label, MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN),
            ),
            (
                "mixed-domain",
                "enabled",
                "disabled",
                {
                    (
                        legacy_label,
                        MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
                    ): "enabled"
                },
                (legacy_label, MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN),
            ),
        )
        for (
            case,
            canonical_user_state,
            canonical_gui_state,
            legacy_overrides,
            legacy_identity,
        ) in cases:
            with self.subTest(case=case):
                case_user_home = self.root / f"orphan-matrix-{case}" / "home"
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths("macos", case_home)
                query_results = (
                    *self.launchd_query_matrix(
                        canonical_user_state,
                        canonical_gui_state,
                        legacy_overrides=legacy_overrides,
                    ),
                    *self.launchd_query_matrix("disabled", "disabled"),
                )
                native_calls: list[list[str]] = []

                def capture_native(
                    args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool | str = False,
                ) -> None:
                    self.assertFalse(dry_run)
                    del allow_fail
                    native_calls.append(args)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=query_results,
                    ) as daemon_query,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=capture_native,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        "macos",
                        dry_run=False,
                        disable=True,
                    )

                query_width = 2 * (1 + len(MODULE.LEGACY_LAUNCHD_LABELS))
                self.assertEqual(daemon_query.call_count, 2 * query_width)
                self.assertFalse(
                    MODULE._scheduler_uninstall_transaction_path(paths).exists()
                )
                assert paths.launchd_plist is not None
                self.assertFalse(paths.launchd_plist.exists())
                legacy_name, legacy_domain = legacy_identity
                self.assertIn(
                    [
                        "launchctl",
                        "bootout",
                        f"{legacy_domain}/{os.getuid()}/{legacy_name}",
                    ],
                    native_calls,
                )

    def test_uninstall_orphan_daemon_uncertainty_and_failures_retain_marker(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            for failure_point in ("prequery", "native", "postquery"):
                with self.subTest(
                    platform=platform_name,
                    failure_point=failure_point,
                ):
                    case_user_home = (
                        self.root
                        / f"orphan-failure-{platform_name}-{failure_point}"
                        / "home"
                    )
                    case_user_home.mkdir(parents=True)
                    case_home = case_user_home / ".codex"
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    queries = (
                        (
                            MODULE.SchedulerDaemonQuery(
                                "unavailable",
                                "daemon query denied",
                            ),
                        )
                        if failure_point == "prequery"
                        else (
                            MODULE.SchedulerDaemonQuery("enabled"),
                            MODULE.SchedulerDaemonQuery("active-disabled"),
                        )
                    )
                    native_failure = (
                        MODULE.SyncError("native cleanup failed")
                        if failure_point == "native"
                        else None
                    )
                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_scheduler_daemon_enabled",
                            side_effect=queries,
                        ) as daemon_query,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=native_failure,
                        ) as native_command,
                        contextlib.redirect_stdout(output),
                        self.assertRaises(MODULE.SyncError) as raised,
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                    marker = MODULE._scheduler_uninstall_transaction_path(paths)
                    self.assertTrue(marker.is_file())
                    self.assertNotIn("removed ", output.getvalue())
                    if failure_point == "prequery":
                        self.assertEqual(
                            raised.exception.code,
                            "scheduler-uninstall-incomplete",
                        )
                        native_command.assert_not_called()
                    elif failure_point == "native":
                        self.assertEqual(str(raised.exception), "native cleanup failed")
                        self.assertEqual(daemon_query.call_count, 1)
                    else:
                        self.assertEqual(
                            raised.exception.code,
                            "scheduler-uninstall-incomplete",
                        )
                        self.assertEqual(daemon_query.call_count, 2)

    def test_uninstall_orphan_publishes_marker_before_native_cleanup(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-marker-failure-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_retain_scheduler_uninstall_transaction",
                        side_effect=MODULE.SyncError(
                            "marker publication failed",
                            code="scheduler-uninstall-incomplete",
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        side_effect=AssertionError(
                            "daemon query ran before durable marker publication"
                        ),
                    ) as daemon_query,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "native cleanup ran before durable marker publication"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "marker publication failed",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                daemon_query.assert_not_called()
                native_command.assert_not_called()
                self.assertTrue(MODULE._scheduler_config_parent(paths).is_dir())
                self.assertFalse(
                    MODULE._scheduler_uninstall_transaction_path(paths).exists()
                )

    def test_uninstall_orphan_retries_from_durable_marker(self) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = self.root / f"orphan-retry-{platform_name}" / "home"
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                marker = MODULE._scheduler_uninstall_transaction_path(paths)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        return_value=MODULE.SchedulerDaemonQuery(
                            "unavailable",
                            "daemon query denied",
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "native cleanup ran after an inconclusive pre-query"
                        ),
                    ) as first_native,
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(MODULE.SyncError) as first_failure,
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertEqual(
                    first_failure.exception.code,
                    "scheduler-uninstall-incomplete",
                )
                first_native.assert_not_called()
                self.assertTrue(marker.is_file())
                self.assertTrue(MODULE._scheduler_config_parent(paths).is_dir())

                native_calls: list[list[str]] = []

                def capture_native(
                    args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool | str = False,
                ) -> None:
                    self.assertFalse(dry_run)
                    del allow_fail
                    native_calls.append(args)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        side_effect=(
                            MODULE.SchedulerDaemonQuery("enabled"),
                            MODULE.SchedulerDaemonQuery("disabled"),
                        ),
                    ) as daemon_query,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=capture_native,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertEqual(daemon_query.call_count, 2)
                self.assertFalse(marker.exists())
                if platform_name == "macos":
                    domain = f"gui/{os.getuid()}"
                    self.assertIn(
                        [
                            "launchctl",
                            "bootout",
                            f"{domain}/{MODULE.LAUNCHD_LABEL}",
                        ],
                        native_calls,
                    )
                else:
                    self.assertIn(
                        [
                            "systemctl",
                            "--user",
                            "disable",
                            "--now",
                            f"{MODULE.SYSTEMD_UNIT}.timer",
                        ],
                        native_calls,
                    )

    def test_uninstall_orphan_binds_config_absence_across_daemon_query(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-config-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                payload = f"concurrent {platform_name} config\n".encode()
                native_queries: list[list[str]] = []

                def appear_during_query(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    native_queries.append(args)
                    target.write_bytes(payload)
                    target.chmod(0o600)
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "enabled\n",
                        "",
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=appear_during_query,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "native cleanup ran after scheduler config appeared"
                        ),
                    ) as native_command,
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "appeared after initial absence",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertEqual(len(native_queries), 1)
                native_command.assert_not_called()
                self.assertEqual(target.read_bytes(), payload)
                self.assertTrue(
                    MODULE._scheduler_uninstall_transaction_path(paths).is_file()
                )

    def test_uninstall_missing_parent_race_fails_closed_and_preserves_appearance(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"missing-parent-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                parent = MODULE._scheduler_config_parent(paths)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                payload = f"concurrent {platform_name} config\n".encode("utf-8")
                first_component = "Library" if platform_name == "macos" else ".config"
                real_open = MODULE.os.open
                injected = False

                def observe_missing_then_appear(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal injected
                    if (
                        not injected
                        and os.fspath(path) == first_component
                        and dir_fd is not None
                    ):
                        injected = True
                        parent.mkdir(parents=True)
                        target.write_bytes(payload)
                        target.chmod(0o600)
                        raise FileNotFoundError(first_component)
                    return real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=observe_missing_then_appear,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "missing-parent no-op acquired the install lock"
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "missing-parent no-op ran a native command"
                        ),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "appeared after a missing observation",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), payload)

    def test_uninstall_config_parent_uncertainty_fails_closed(self) -> None:
        for platform_name in ("macos", "linux"):
            for mutation, expected_error in (
                ("file", "is not a directory"),
                ("symlink", "is a symlink"),
                ("unreadable", "is unreadable"),
            ):
                with self.subTest(platform=platform_name, mutation=mutation):
                    case_user_home = (
                        self.root
                        / f"uncertain-parent-{platform_name}-{mutation}"
                        / "home"
                    )
                    case_home = case_user_home / ".codex"
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    parent = MODULE._scheduler_config_parent(paths)
                    parent.parent.mkdir(parents=True)
                    if mutation == "file":
                        parent.write_bytes(b"foreign parent object\n")
                    elif mutation == "symlink":
                        target = parent.with_name(parent.name + ".target")
                        target.mkdir()
                        parent.symlink_to(target, target_is_directory=True)
                    else:
                        parent.mkdir()

                    real_open = MODULE.os.open
                    real_stat = MODULE.os.stat

                    def reject_parent_open(
                        path: os.PathLike[str] | str,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        if (
                            mutation == "unreadable"
                            and os.fspath(path) == parent.name
                            and dir_fd is not None
                        ):
                            raise PermissionError(parent.name)
                        return real_open(
                            path,
                            flags,
                            mode,
                            dir_fd=dir_fd,
                        )

                    def reject_parent_read(
                        path: os.PathLike[str] | str,
                        *args: object,
                        **kwargs: object,
                    ) -> os.stat_result:
                        if (
                            mutation == "unreadable"
                            and os.fspath(path) == parent.name
                            and kwargs.get("dir_fd") is not None
                        ):
                            raise PermissionError(parent.name)
                        return real_stat(path, *args, **kwargs)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "open",
                            side_effect=reject_parent_open,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "stat",
                            side_effect=reject_parent_read,
                        ),
                        mock.patch.object(
                            MODULE,
                            "installation_lock",
                            side_effect=AssertionError(
                                "uncertain parent acquired the install lock"
                            ),
                        ) as install_lock,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=AssertionError(
                                "uncertain parent ran a native command"
                            ),
                        ) as native_command,
                        self.assertRaisesRegex(MODULE.SyncError, expected_error),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                    install_lock.assert_not_called()
                    native_command.assert_not_called()

    def test_uninstall_intermediate_config_parent_symlink_fails_closed(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"intermediate-symlink-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                if platform_name == "macos":
                    link = case_user_home / "Library"
                    link_target = case_user_home / "foreign-library"
                    config_parent = link_target / "LaunchAgents"
                    config_name = f"{MODULE.LAUNCHD_LABEL}.plist"
                else:
                    config_root = case_user_home / ".config"
                    config_root.mkdir()
                    link = config_root / "systemd"
                    link_target = case_user_home / "foreign-systemd"
                    config_parent = link_target / "user"
                    config_name = f"{MODULE.SYSTEMD_UNIT}.service"
                config_parent.mkdir(parents=True)
                link.symlink_to(link_target, target_is_directory=True)
                foreign_config = config_parent / config_name
                payload = f"foreign {platform_name} scheduler\n".encode("utf-8")
                foreign_config.write_bytes(payload)
                foreign_config.chmod(0o600)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "intermediate symlink acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "intermediate symlink ran a native command"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "component is a symlink",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertEqual(foreign_config.read_bytes(), payload)

    def test_uninstall_config_parent_component_replacement_fails_closed(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"component-replacement-{platform_name}" / "home"
                )
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                parent = MODULE._scheduler_config_parent(paths)
                parent.mkdir(parents=True)
                first_component = "Library" if platform_name == "macos" else ".config"
                original_component = case_user_home / first_component
                displaced_component = original_component.with_name(
                    original_component.name + ".displaced"
                )
                real_open = MODULE.os.open
                injected = False

                def replace_opened_component(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal injected
                    file_descriptor = real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )
                    if (
                        not injected
                        and os.fspath(path) == first_component
                        and dir_fd is not None
                    ):
                        injected = True
                        original_component.rename(displaced_component)
                        original_component.mkdir()
                    return file_descriptor

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=replace_opened_component,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "component replacement acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "component replacement ran a native command"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "component identity changed",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertTrue(injected)
                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertTrue(displaced_component.exists())

    def test_uninstall_binds_original_scheduler_configs_across_native_calls(
        self,
    ) -> None:
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("owner", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        case_index = 0
        for platform_name in ("macos", "linux"):
            targets = ("plist",) if platform_name == "macos" else ("service", "timer")
            action_indexes = range(2) if platform_name == "macos" else range(1)
            for target_kind in targets:
                for action_index in action_indexes:
                    for mutation, expected_error in mutations:
                        case_index += 1
                        with self.subTest(
                            platform=platform_name,
                            target=target_kind,
                            action=action_index,
                            mutation=mutation,
                        ):
                            case_user_home = (
                                self.root / f"uninstall-{case_index}" / "home"
                            )
                            case_home = case_user_home / ".codex"
                            runner = case_home / "bin" / "codex-personal-sync"
                            runner.parent.mkdir(parents=True)
                            runner.write_text(
                                "#!/bin/sh\nexit 0\n",
                                encoding="utf-8",
                            )
                            runner.chmod(0o755)
                            with mock.patch.object(
                                MODULE.Path,
                                "home",
                                return_value=case_user_home,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    MODULE.install_scheduler(
                                        case_home,
                                        "owner/public-sync",
                                        17,
                                        platform_name,
                                        None,
                                        dry_run=False,
                                        enable=False,
                                    )
                                paths = MODULE._scheduler_paths(
                                    platform_name,
                                    case_home,
                                )
                            if target_kind == "plist":
                                target = paths.launchd_plist
                            elif target_kind == "service":
                                target = paths.systemd_service
                            else:
                                target = paths.systemd_timer
                            assert target is not None
                            original = target.read_bytes()
                            original_metadata = target.stat()
                            original_identity = (
                                original_metadata.st_dev,
                                original_metadata.st_ino,
                            )
                            original_mode = stat.S_IMODE(original_metadata.st_mode)
                            real_read = MODULE._read_managed_state_bytes
                            real_fstat = MODULE.os.fstat
                            native_calls = 0
                            force_owner = False
                            force_unreadable = False

                            def mutate_during_uninstall(
                                _args: list[str],
                                *,
                                dry_run: bool,
                                allow_fail: bool = False,
                            ) -> None:
                                del dry_run, allow_fail
                                nonlocal force_owner
                                nonlocal force_unreadable
                                nonlocal native_calls
                                current_call = native_calls
                                native_calls += 1
                                if current_call != action_index:
                                    return
                                if mutation == "replacement":
                                    candidate = target.with_name(
                                        target.name + ".replacement"
                                    )
                                    candidate.write_bytes(original)
                                    candidate.chmod(original_mode)
                                    os.replace(candidate, target)
                                elif mutation == "content":
                                    payload = bytearray(original)
                                    payload[len(payload) // 2] ^= 1
                                    target.write_bytes(payload)
                                    target.chmod(original_mode)
                                elif mutation == "mode":
                                    target.chmod(
                                        0o644 if original_mode != 0o644 else 0o600
                                    )
                                elif mutation == "owner":
                                    force_owner = True
                                elif mutation == "parent":
                                    displaced = target.parent.with_name(
                                        target.parent.name + ".displaced"
                                    )
                                    target.parent.rename(displaced)
                                    target.parent.mkdir()
                                elif mutation == "missing":
                                    target.unlink()
                                else:
                                    force_unreadable = True

                            def fstat_with_owner(
                                file_fd: int,
                            ) -> os.stat_result:
                                metadata = real_fstat(file_fd)
                                if (
                                    force_owner
                                    and (metadata.st_dev, metadata.st_ino)
                                    == original_identity
                                ):
                                    fields = list(metadata)
                                    fields[4] = metadata.st_uid + 1
                                    return os.stat_result(fields)
                                return metadata

                            def fail_bound_read(
                                file_fd: int,
                                path: Path,
                                maximum_bytes: int = (MODULE.MAX_MANAGED_STATE_BYTES),
                            ) -> bytes:
                                if force_unreadable and path == target:
                                    raise MODULE.SyncError(
                                        "injected bound read failure"
                                    )
                                return real_read(
                                    file_fd,
                                    path,
                                    maximum_bytes,
                                )

                            output = io.StringIO()
                            with (
                                mock.patch.object(
                                    MODULE.Path,
                                    "home",
                                    return_value=case_user_home,
                                ),
                                mock.patch.object(
                                    MODULE,
                                    "_run_native_command",
                                    side_effect=mutate_during_uninstall,
                                ),
                                mock.patch.object(
                                    MODULE.os,
                                    "fstat",
                                    side_effect=fstat_with_owner,
                                ),
                                mock.patch.object(
                                    MODULE,
                                    "_read_managed_state_bytes",
                                    side_effect=fail_bound_read,
                                ),
                                contextlib.redirect_stdout(output),
                                self.assertRaisesRegex(
                                    MODULE.SyncError,
                                    expected_error,
                                ),
                            ):
                                MODULE.uninstall_scheduler(
                                    case_home,
                                    platform_name,
                                    dry_run=False,
                                    disable=True,
                                )

                            self.assertEqual(
                                native_calls,
                                action_index + 1,
                            )
                            self.assertNotIn(
                                "removed ",
                                output.getvalue(),
                            )
                            if mutation == "replacement":
                                self.assertEqual(target.read_bytes(), original)

    def test_linux_uninstall_preserves_foreign_systemd_drop_ins(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
            enable=False,
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        service_drop_in = paths.systemd_service.with_name(
            paths.systemd_service.name + ".d"
        )
        timer_drop_in = paths.systemd_timer.with_name(paths.systemd_timer.name + ".d")
        service_drop_in.mkdir()
        timer_drop_in.mkdir()
        service_override = service_drop_in / "foreign.conf"
        timer_override = timer_drop_in / "foreign.conf"
        service_override.write_text("[Service]\nNice=5\n", encoding="utf-8")
        timer_override.write_text("[Timer]\nRandomizedDelaySec=1m\n", encoding="utf-8")

        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: ["/usr/bin/systemctl", *args[1:]],
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )

        self.assertFalse(paths.systemd_service.exists())
        self.assertFalse(paths.systemd_timer.exists())
        self.assertEqual(
            service_override.read_text(encoding="utf-8"), "[Service]\nNice=5\n"
        )
        self.assertEqual(
            timer_override.read_text(encoding="utf-8"),
            "[Timer]\nRandomizedDelaySec=1m\n",
        )

    def test_linux_uninstall_revalidates_unit_absence_around_daemon_reload(
        self,
    ) -> None:
        case_index = 0
        for target_kind in ("service", "timer"):
            for appearance_phase in ("before", "during"):
                case_index += 1
                with self.subTest(
                    target=target_kind,
                    phase=appearance_phase,
                ):
                    case_user_home = self.root / f"reload-absence-{case_index}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                "linux",
                                None,
                                dry_run=False,
                                enable=False,
                            )
                        paths = MODULE._scheduler_paths("linux", case_home)
                    target = (
                        paths.systemd_service
                        if target_kind == "service"
                        else paths.systemd_timer
                    )
                    other = (
                        paths.systemd_timer
                        if target_kind == "service"
                        else paths.systemd_service
                    )
                    assert target is not None
                    assert other is not None
                    payload = (f"reappeared {target_kind} {appearance_phase}\n").encode(
                        "utf-8"
                    )
                    native_calls = 0
                    real_report = MODULE._report_preserved_systemd_drop_ins

                    def appear_before_reload(
                        selected_paths: MODULE.SchedulerPaths,
                    ) -> None:
                        real_report(selected_paths)
                        if appearance_phase == "before":
                            target.write_bytes(payload)
                            target.chmod(0o600)

                    def appear_during_reload(
                        args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        native_calls += 1
                        if appearance_phase == "during" and args[-1] == "daemon-reload":
                            target.write_bytes(payload)
                            target.chmod(0o600)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_report_preserved_systemd_drop_ins",
                            side_effect=appear_before_reload,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=appear_during_reload,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "reappeared after conditional removal",
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            "linux",
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(
                        native_calls,
                        1 if appearance_phase == "before" else 2,
                    )
                    self.assertEqual(target.read_bytes(), payload)
                    self.assertFalse(other.exists())
                    self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_commit_revalidates_shared_parent_group(self) -> None:
        for platform_name in ("macos", "linux"):
            for mutation in ("during-fsync", "during-second-pass"):
                with self.subTest(
                    platform=platform_name,
                    mutation=mutation,
                ):
                    case_user_home = (
                        self.root / f"commit-group-{platform_name}-{mutation}" / "home"
                    )
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                platform_name,
                                None,
                                dry_run=False,
                                enable=False,
                            )
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(
                            platform_name,
                            case_home,
                        )
                    target = (
                        paths.launchd_plist
                        if platform_name == "macos"
                        else paths.systemd_service
                    )
                    assert target is not None
                    marker = MODULE._scheduler_uninstall_transaction_path(paths)
                    original = target.read_bytes()
                    real_commit = MODULE._commit_scheduler_uninstall_transaction
                    real_fsync = MODULE.os.fsync
                    real_revalidate = MODULE._revalidate_launchd_activation_binding
                    commit_active = False
                    injected = False
                    marker_checks = 0
                    commit_parent_fds: list[int] = []

                    def reappear_target() -> None:
                        nonlocal injected
                        target.write_bytes(original)
                        target.chmod(0o600)
                        injected = True

                    def commit_and_arm(
                        marker_binding: MODULE.SchedulerActivationBinding,
                        *,
                        related_bindings: tuple[
                            MODULE.SchedulerActivationBinding,
                            ...,
                        ],
                    ) -> None:
                        nonlocal commit_active
                        commit_active = True
                        real_commit(
                            marker_binding,
                            related_bindings=related_bindings,
                        )

                    def sync_then_reappear(file_fd: int) -> None:
                        real_fsync(file_fd)
                        if (
                            commit_active
                            and mutation == "during-fsync"
                            and not injected
                        ):
                            reappear_target()

                    def revalidate_and_interleave(
                        binding: MODULE.SchedulerActivationBinding,
                        *,
                        boundary: str,
                    ) -> None:
                        nonlocal marker_checks
                        if commit_active and boundary == (
                            "after parent sync before uninstall transaction commit"
                        ):
                            commit_parent_fds.append(binding.parent_fd)
                            if (
                                mutation == "during-second-pass"
                                and binding.path == marker
                            ):
                                marker_checks += 1
                                if marker_checks == 2:
                                    reappear_target()
                        real_revalidate(binding, boundary=boundary)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_commit_scheduler_uninstall_transaction",
                            side_effect=commit_and_arm,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "fsync",
                            side_effect=sync_then_reappear,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_revalidate_launchd_activation_binding",
                            side_effect=revalidate_and_interleave,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "reappeared after conditional removal",
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=False,
                        )

                    self.assertTrue(injected)
                    self.assertTrue(marker.is_file())
                    self.assertTrue(target.is_file())
                    self.assertTrue(commit_parent_fds)
                    self.assertEqual(len(set(commit_parent_fds)), 1)

    def test_uninstall_native_failures_retain_transaction_and_configs(
        self,
    ) -> None:
        success = subprocess.CompletedProcess(
            ["scheduler-action"],
            0,
            "",
            "",
        )
        failures: tuple[
            tuple[str, str, int, BaseException | subprocess.CompletedProcess[str]],
            ...,
        ] = (
            (
                "macos",
                "timeout",
                0,
                MODULE.SyncError(
                    "scheduler native command exceeded its monotonic deadline",
                    code="scheduler-timeout",
                ),
            ),
            (
                "macos",
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["launchctl", "bootout"],
                    1,
                    "",
                    "Operation not permitted",
                ),
            ),
            (
                "macos",
                "unknown",
                1,
                subprocess.CompletedProcess(
                    ["launchctl", "disable"],
                    1,
                    "",
                    "Input/output error",
                ),
            ),
            (
                "linux",
                "timeout",
                0,
                MODULE.SyncError(
                    "scheduler native command exceeded its monotonic deadline",
                    code="scheduler-timeout",
                ),
            ),
            (
                "linux",
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["systemctl", "disable"],
                    1,
                    "",
                    "Permission denied",
                ),
            ),
            (
                "linux",
                "unknown",
                0,
                subprocess.CompletedProcess(
                    ["systemctl", "disable"],
                    1,
                    "",
                    "Unit operation failed",
                ),
            ),
        )
        for platform_name, failure_kind, failure_call, failure in failures:
            with self.subTest(
                platform=platform_name,
                failure=failure_kind,
            ):
                case_user_home = (
                    self.root / f"native-{platform_name}-{failure_kind}" / "home"
                )
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(
                        platform_name,
                        case_home,
                    )
                    native_results: list[
                        BaseException | subprocess.CompletedProcess[str]
                    ] = [success] * failure_call + [failure]
                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=native_results,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaises(MODULE.SyncError),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                config_paths = (
                    (paths.launchd_plist,)
                    if platform_name == "macos"
                    else (paths.systemd_service, paths.systemd_timer)
                )
                self.assertTrue(
                    all(path is not None and path.is_file() for path in config_paths)
                )
                self.assertTrue(
                    MODULE._scheduler_uninstall_transaction_path(paths).is_file()
                )
                self.assertNotIn("removed ", output.getvalue())

    def test_macos_install_and_uninstall_accept_gui_disable_domain_absence(
        self,
    ) -> None:
        uid = os.getuid()
        gui_domain = f"gui/{uid}"
        for operation in ("install", "uninstall"):
            with self.subTest(operation=operation):
                case_user_home = self.root / f"gui-domain-absent-{operation}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths("macos", case_home)
                    if operation == "uninstall":
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                "macos",
                                None,
                                dry_run=False,
                                enable=False,
                            )

                native_calls: list[list[str]] = []

                def gui_domain_absent(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    native_calls.append(args)
                    if (
                        len(args) == 3
                        and args[0] == "launchctl"
                        and args[2].startswith(f"{gui_domain}/")
                    ):
                        if args[1] == "bootout":
                            return subprocess.CompletedProcess(
                                args,
                                125,
                                "",
                                (
                                    "Boot-out failed: 125: "
                                    "Domain does not support specified action"
                                ),
                            )
                        if args[1] == "disable":
                            return subprocess.CompletedProcess(
                                args,
                                125,
                                "",
                                (
                                    "Could not disable service: 125: "
                                    "Domain does not support specified action"
                                ),
                            )
                    return subprocess.CompletedProcess(args, 0, "", "")

                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=gui_domain_absent,
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    if operation == "install":
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=True,
                        )
                    else:
                        MODULE.uninstall_scheduler(
                            case_home,
                            "macos",
                            dry_run=False,
                            disable=True,
                        )

                gui_disable_targets = [
                    args[2]
                    for args in native_calls
                    if len(args) == 3
                    and args[:2] == ["launchctl", "disable"]
                    and args[2].startswith(f"{gui_domain}/")
                ]
                self.assertEqual(
                    len(gui_disable_targets),
                    1 + len(MODULE.LEGACY_LAUNCHD_LABELS),
                )
                self.assertEqual(
                    output.getvalue().count("ignored already-absent scheduler command"),
                    2 * (1 + len(MODULE.LEGACY_LAUNCHD_LABELS)),
                )
                assert paths.launchd_plist is not None
                self.assertEqual(
                    paths.launchd_plist.exists(),
                    operation == "install",
                )
                self.assertFalse(
                    MODULE._scheduler_activation_transaction_path(paths).exists()
                )
                self.assertFalse(
                    MODULE._scheduler_uninstall_transaction_path(paths).exists()
                )

    def test_uninstall_accepts_only_precise_absence_evidence(self) -> None:
        uid = os.getuid()
        legacy_label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        accepted = (
            (
                [
                    "launchctl",
                    "bootout",
                    "gui/501",
                    "/tmp/scheduler.plist",
                ],
                "Boot-out failed: 3: No such process",
            ),
            (
                ["launchctl", "disable", "gui/501/example"],
                "Could not find specified service",
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"user/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for uid: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                "Could not print domain: 125: Domain does not support specified action",
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Could not disable service: 125: "
                    "Domain does not support specified action"
                ),
            ),
            (
                ["launchctl", "bootout", f"gui/{uid}/{legacy_label}"],
                (
                    "Bad request.\n"
                    f'Could not find service "{legacy_label}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "bootout",
                    f"gui/{uid}",
                    f"/tmp/{legacy_label}.plist",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{legacy_label}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (
                    "Failed to disable unit: Unit "
                    f"{MODULE.SYSTEMD_UNIT}.timer not loaded."
                ),
            ),
        )
        for args, stderr in accepted:
            with self.subTest(args=args):
                completed = subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    stderr,
                )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda selected: selected,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        return_value=completed,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE._run_native_command(
                        args,
                        dry_run=False,
                        allow_fail=MODULE.NATIVE_FAILURE_ALREADY_ABSENT,
                    )

        rejected = (
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (
                    "Failed to disable unit: Unit file "
                    f"{MODULE.SYSTEMD_UNIT}.timer does not exist."
                ),
            ),
            (
                ["launchctl", "disable", "gui/501/example"],
                "Permission denied: Could not find specified service",
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    'Could not find service "mismatched.label" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL.upper()}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid + 1}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid + 1}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid}\n"
                    "additional diagnostic"
                ),
            ),
            (
                [
                    "launchctl",
                    "bootout",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Could not disable service: 125: "
                    "Domain does not support specified action"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"user/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Could not disable service: 125: "
                    "Domain does not support specified action"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/unmanaged.scheduler",
                ],
                (
                    "Could not disable service: 125: "
                    "Domain does not support specified action"
                ),
            ),
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (f"Permission denied: Unit {MODULE.SYSTEMD_UNIT}.timer not loaded."),
            ),
        )
        for args, stderr in rejected:
            with self.subTest(stderr=stderr):
                completed = subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    stderr,
                )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda selected: selected,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        return_value=completed,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        re.escape(stderr),
                    ),
                ):
                    MODULE._run_native_command(
                        args,
                        dry_run=False,
                        allow_fail=MODULE.NATIVE_FAILURE_ALREADY_ABSENT,
                    )

    def test_linux_uninstall_reload_failure_is_reported_and_recoverable(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        marker = MODULE._scheduler_uninstall_transaction_path(paths)
        failed_results = (
            subprocess.CompletedProcess(
                ["systemctl", "disable"],
                0,
                "",
                "",
            ),
            subprocess.CompletedProcess(
                ["systemctl", "daemon-reload"],
                1,
                "",
                "Permission denied",
            ),
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=failed_results,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(MODULE.SyncError, "Permission denied"),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )

        self.assertFalse(paths.systemd_service.exists())
        self.assertFalse(paths.systemd_timer.exists())
        self.assertTrue(marker.is_file())
        self.assertNotIn("removed ", output.getvalue())
        with (
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
            mock.patch.object(
                MODULE,
                "audit_active_skills",
                return_value=[],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=False,
            )
        self.assertEqual(
            report.failure_code,
            "scheduler-uninstall-incomplete",
        )
        uninstall_issues = [
            issue for issue in issues if issue.code == "scheduler-uninstall-incomplete"
        ]
        self.assertEqual(len(uninstall_issues), 1)
        self.assertEqual(uninstall_issues[0].path, marker)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "incomplete uninstall transaction",
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

        recovered_results = (
            subprocess.CompletedProcess(
                ["systemctl", "disable"],
                1,
                "",
                (
                    "Failed to disable unit: Unit "
                    f"{MODULE.SYSTEMD_UNIT}.timer not loaded."
                ),
            ),
            subprocess.CompletedProcess(
                ["systemctl", "daemon-reload"],
                0,
                "",
                "",
            ),
        )
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                side_effect=(
                    MODULE.SchedulerDaemonQuery("enabled"),
                    MODULE.SchedulerDaemonQuery("disabled"),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=recovered_results,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )
        self.assertFalse(marker.exists())

    def test_bounded_scheduler_process_enforces_raw_byte_limits(self) -> None:
        limit = 4096
        with (
            mock.patch.object(MODULE, "MAX_SCHEDULER_NATIVE_STDOUT_BYTES", limit),
            mock.patch.object(MODULE, "MAX_SCHEDULER_NATIVE_STDERR_BYTES", limit),
        ):
            exact = MODULE._run_bounded_scheduler_process(
                [
                    sys.executable,
                    "-c",
                    (f"import os;os.write(1,b'o'*{limit});os.write(2,b'e'*{limit})"),
                ],
                timeout_seconds=5.0,
            )
            self.assertEqual(len(exact.stdout.encode("utf-8")), limit)
            self.assertEqual(len(exact.stderr.encode("utf-8")), limit)

            for stream_name, file_descriptor in (("stdout", 1), ("stderr", 2)):
                with self.subTest(stream=stream_name):
                    processes: list[subprocess.Popen[bytes]] = []
                    real_popen = subprocess.Popen

                    def capture_process(args, **kwargs):
                        process = real_popen(args, **kwargs)
                        processes.append(process)
                        return process

                    with (
                        mock.patch.object(
                            MODULE.subprocess,
                            "Popen",
                            side_effect=capture_process,
                        ),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            f"{stream_name} exceeds the {limit}-byte raw output limit",
                        ) as raised,
                    ):
                        MODULE._run_bounded_scheduler_process(
                            [
                                sys.executable,
                                "-c",
                                (
                                    "import os,time;"
                                    f"os.write({file_descriptor},b'x'*{limit + 1});"
                                    "time.sleep(30)"
                                ),
                            ],
                            timeout_seconds=5.0,
                        )

                    self.assertEqual(raised.exception.code, "scheduler-output-limit")
                    self.assertEqual(len(processes), 1)
                    self.assertIsNotNone(processes[0].poll())

    def test_bounded_scheduler_process_times_out_and_reaps_child(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def capture_process(args, **kwargs):
            process = real_popen(args, **kwargs)
            processes.append(process)
            return process

        with (
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_process,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exceeded its monotonic deadline",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=0.05,
            )

        self.assertEqual(raised.exception.code, "scheduler-timeout")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_scheduler_guardian_fences_descendant_after_target_exit(self) -> None:
        fifo = self.root / "guardian-liveness.fifo"
        os.mkfifo(fifo, 0o600)
        read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            completed = MODULE._run_bounded_scheduler_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,subprocess,sys;"
                        "fd=os.open(sys.argv[1],os.O_WRONLY);"
                        "os.write(fd,b'R');"
                        "subprocess.Popen("
                        "[sys.executable,'-c','import time;time.sleep(30)'],"
                        "stdin=subprocess.DEVNULL,"
                        "stdout=subprocess.DEVNULL,"
                        "stderr=subprocess.DEVNULL,"
                        "pass_fds=(fd,));"
                        "os.close(fd)"
                    ),
                    str(fifo),
                ],
                timeout_seconds=5.0,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(os.read(read_fd, 1), b"R")
            _wait_for_scheduler_guardian_fifo_eof(
                read_fd,
                deadline=(
                    time.monotonic()
                    + _SCHEDULER_DOCTOR_TEST_GUARDIAN_EOF_TIMEOUT_SECONDS
                ),
            )
        finally:
            os.close(read_fd)

    def test_scheduler_guardian_fifo_eof_wait_requires_writer_exit(self) -> None:
        fifo = self.root / "guardian-liveness-negative-control.fifo"
        os.mkfifo(fifo, 0o600)
        read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        write_fd = -1
        writer: subprocess.Popen[bytes] | None = None
        try:
            write_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            writer = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(write_fd,),
            )
            os.close(write_fd)
            write_fd = -1
            with self.assertRaisesRegex(
                AssertionError,
                "still retains the FIFO liveness writer",
            ):
                _wait_for_scheduler_guardian_fifo_eof(
                    read_fd,
                    deadline=time.monotonic() + 0.01,
                )
            writer.kill()
            writer.wait(timeout=5.0)
            _wait_for_scheduler_guardian_fifo_eof(
                read_fd,
                deadline=time.monotonic() + 1.0,
            )
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            if writer is not None and writer.poll() is None:
                writer.kill()
                writer.wait(timeout=5.0)
            os.close(read_fd)

    def test_bounded_scheduler_selector_close_failure_preserves_primary(
        self,
    ) -> None:
        process = MODULE._spawn_guarded_process(
            [sys.executable, "-c", "import os;os.write(1,b'123456789')"],
            deadline=time.monotonic() + 5.0,
            process_label="test scheduler",
            unavailable_code="test-unavailable",
            unavailable_message="test executable unavailable",
        )
        real_selector = MODULE.selectors.DefaultSelector
        selector_calls = 0

        class CloseFailingSelector:
            def __init__(self, inner) -> None:
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def close(self) -> None:
                self.inner.close()
                raise ValueError("injected scheduler selector close failure")

        def selector_factory():
            nonlocal selector_calls
            selector_calls += 1
            if selector_calls == 1:
                return CloseFailingSelector(real_selector())
            return real_selector()

        with (
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                return_value=process,
            ),
            mock.patch.object(
                MODULE.selectors,
                "DefaultSelector",
                side_effect=selector_factory,
            ),
            mock.patch.object(MODULE, "MAX_SCHEDULER_NATIVE_STDOUT_BYTES", 8),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "stdout exceeds.*selector close failure",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "unused"],
                timeout_seconds=5.0,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertIsInstance(raised.exception.__cause__, MODULE.SyncError)
        self.assertEqual(
            raised.exception.__cause__.code,
            "scheduler-output-limit",
        )
        self.assertEqual(process.returncode, -MODULE.signal.SIGKILL)

    def test_scheduler_spawn_cleanup_failure_cannot_be_ignored(self) -> None:
        primary = MODULE.SyncError(
            "injected guardian cleanup failure",
            code="process-guardian-cleanup-inconclusive",
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                return_value=[sys.executable, "-c", "pass"],
            ),
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                side_effect=primary,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "guardian cleanup was inconclusive",
            ) as raised,
        ):
            MODULE._run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=False,
                allow_fail=True,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertNotIn("ignored failed command", output.getvalue())

    def test_scheduler_maps_guardian_operation_deadline_to_lane_taxonomy(
        self,
    ) -> None:
        primary = MODULE.SyncError(
            "scheduler native command exceeded its monotonic deadline",
            code="process-guardian-operation-timeout",
        )
        with (
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                side_effect=primary,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "scheduler native command exceeded its monotonic deadline",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "pass"],
                timeout_seconds=1.0,
            )

        self.assertEqual(raised.exception.code, "scheduler-timeout")
        self.assertIs(raised.exception.__cause__, primary)

    def test_bounded_scheduler_process_reaps_child_when_selectors_fail(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen
        real_selector = MODULE.selectors.DefaultSelector
        selector_calls = 0

        def capture_process(args, **kwargs):
            process = real_popen(args, **kwargs)
            processes.append(process)
            return process

        def fail_after_ready_selector():
            nonlocal selector_calls
            selector_calls += 1
            if selector_calls == 1:
                return real_selector()
            raise OSError("simulated selector exhaustion")

        with (
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_process,
            ),
            mock.patch.object(
                MODULE.selectors,
                "DefaultSelector",
                side_effect=fail_after_ready_selector,
            ),
            mock.patch.object(MODULE, "GH_CLEANUP_TIMEOUT_SECONDS", 1.0),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "cleanup was inconclusive.*cleanup selector",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=5.0,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, -9)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)

    def test_scheduler_cleanup_uncertainty_preserves_primary_classification(
        self,
    ) -> None:
        primary = MODULE.SyncError(
            "scheduler stdout exceeded its raw output limit",
            code="scheduler-output-limit",
        )
        incomplete = MODULE._GhCleanupReceipt(
            kill_sent=True,
            child_reaped=False,
            stdout_drained=False,
            stderr_drained=True,
            status_drained=False,
            process_group_fenced=False,
            errors=("simulated cleanup uncertainty",),
        )
        with (
            mock.patch.object(
                MODULE,
                "_cleanup_gh_process_group",
                return_value=incomplete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "scheduler stdout exceeded.*cleanup was inconclusive",
            ) as raised,
        ):
            MODULE._raise_scheduler_failure_after_cleanup(mock.Mock(), primary)

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertIs(raised.exception.__cause__, primary)
        self.assertIn("stdout-not-drained", str(raised.exception))

    def test_scheduler_daemon_query_reports_stream_limit_without_payload(
        self,
    ) -> None:
        payload = "unbounded-native-payload"
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=MODULE.SyncError(
                    f"scheduler stdout exceeded; suppressed {payload}",
                    code="scheduler-output-limit",
                ),
            ) as run,
        ):
            query = MODULE._scheduler_daemon_enabled(
                MODULE.SchedulerPaths(platform="macos")
            )

        self.assertEqual(
            run.call_count,
            2 * (1 + len(MODULE.LEGACY_LAUNCHD_LABELS)),
        )
        self.assertEqual(query.classification, "unavailable")
        self.assertIn("output exceeded its byte limit", query.reason or "")
        self.assertNotIn(payload, query.reason or "")

    def test_scheduler_native_allow_fail_does_not_echo_overflow_payload(self) -> None:
        payload = "unbounded-native-payload"
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=MODULE.SyncError(
                    "scheduler stdout exceeded its raw output limit",
                    code="scheduler-output-limit",
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            MODULE._run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=False,
                allow_fail=True,
            )

        self.assertNotIn(payload, output.getvalue())
        self.assertIn("raw output limit", output.getvalue())

    def test_scheduler_native_allow_fail_propagates_cleanup_uncertainty(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=MODULE.SyncError(
                    "scheduler cleanup could not be proved complete",
                    code="scheduler-cleanup-inconclusive",
                ),
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "scheduler cleanup could not be proved complete",
            ) as raised,
        ):
            MODULE._run_native_command(
                ["launchctl", "bootout", "gui/501/com.example.sync"],
                dry_run=False,
                allow_fail=True,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertEqual(output.getvalue(), "")

    def test_scheduler_daemon_query_classifies_only_explicit_state_evidence(
        self,
    ) -> None:
        cases = (
            ("macos", 0, "", "", None, None, None, "enabled", None),
            (
                "macos",
                113,
                "",
                "Could not find service in domain",
                None,
                None,
                None,
                "disabled",
                "not loaded",
            ),
            (
                "macos",
                1,
                "",
                "Operation not permitted",
                None,
                None,
                None,
                "unavailable",
                "denied",
            ),
            (
                "linux",
                0,
                "enabled\n",
                "",
                0,
                "active\n",
                "",
                "enabled",
                None,
            ),
            (
                "linux",
                0,
                "enabled\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "enabled but not active",
            ),
            (
                "linux",
                0,
                "enabled-runtime\n",
                "",
                0,
                "active\n",
                "",
                "active-disabled",
                "runtime",
            ),
            (
                "linux",
                0,
                "enabled-runtime\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "non-terminal unit state enabled-runtime",
            ),
            (
                "linux",
                1,
                "linked-runtime\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "non-terminal unit state linked-runtime",
            ),
            (
                "linux",
                1,
                "static\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "non-terminal unit state static",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "",
                0,
                "active\n",
                "",
                "active-disabled",
                "activity state active",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "",
                0,
                "inactive\n",
                "",
                "unavailable",
                "contradictory",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "",
                3,
                "inactive\n",
                "",
                "disabled",
                "state disabled",
            ),
            (
                "linux",
                1,
                "",
                "Failed to connect to bus",
                3,
                "inactive\n",
                "",
                "unavailable",
                "user bus",
            ),
            (
                "linux",
                1,
                "unexpected\n",
                "",
                3,
                "inactive\n",
                "",
                "unavailable",
                "no recognized",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "Permission denied",
                3,
                "inactive\n",
                "",
                "unavailable",
                "denied",
            ),
        )
        for (
            platform_name,
            enable_returncode,
            enable_stdout,
            enable_stderr,
            active_returncode,
            active_stdout,
            active_stderr,
            classification,
            reason,
        ) in cases:
            with self.subTest(
                platform=platform_name,
                enable_returncode=enable_returncode,
                enable_stdout=enable_stdout,
                enable_stderr=enable_stderr,
                active_returncode=active_returncode,
                active_stdout=active_stdout,
                active_stderr=active_stderr,
            ):
                completed = subprocess.CompletedProcess(
                    ["scheduler-query"],
                    enable_returncode,
                    enable_stdout,
                    enable_stderr,
                )
                results = [completed]
                if platform_name == "macos":
                    if enable_returncode == 0:
                        results = self.launchd_query_matrix("enabled", "disabled")
                    elif "not permitted" in enable_stderr.casefold():
                        results = self.launchd_query_matrix("denied", "disabled")
                    else:
                        results = self.launchd_query_matrix("disabled", "disabled")
                else:
                    results.append(
                        subprocess.CompletedProcess(
                            ["scheduler-activity-query"],
                            active_returncode,
                            active_stdout,
                            active_stderr,
                        )
                    )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=results,
                    ) as run,
                ):
                    query = MODULE._scheduler_daemon_enabled(
                        MODULE.SchedulerPaths(platform=platform_name)
                    )
                if platform_name == "macos":
                    self.assertEqual(
                        [call.args[0] for call in run.call_args_list],
                        [
                            [
                                "launchctl",
                                "print",
                                f"{domain}/{os.getuid()}/{label}",
                            ]
                            for label in (
                                MODULE.LAUNCHD_LABEL,
                                *MODULE.LEGACY_LAUNCHD_LABELS,
                            )
                            for domain in (
                                MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
                                MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
                            )
                        ],
                    )
                self.assertEqual(query.classification, classification)
                self.assertEqual(
                    query.enabled,
                    (
                        True
                        if classification == "enabled"
                        else False
                        if classification
                        in {
                            "active-disabled",
                            "disabled",
                            "enabled-inactive",
                        }
                        else None
                    ),
                )
                if reason is not None:
                    self.assertIn(reason, query.reason or "")

    def test_macos_daemon_query_requires_configured_domain_and_no_duplicate(
        self,
    ) -> None:
        legacy_config = MODULE.SchedulerConfig(
            platform="macos",
            config_paths=(self.root / "legacy.plist",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
            launchd_domain=MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
        )
        legacy_audit = MODULE.SchedulerConfigAudit(
            config=legacy_config,
            snapshots=(),
        )
        background_config = MODULE.SchedulerConfig(
            platform="macos",
            config_paths=(self.root / "background.plist",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
            launchd_domain=MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
        )
        background_audit = MODULE.SchedulerConfigAudit(
            config=background_config,
            snapshots=(),
        )
        cases = (
            (
                "background-enabled",
                None,
                "enabled",
                "disabled",
                "enabled",
                None,
            ),
            (
                "both-disabled",
                None,
                "disabled",
                "disabled",
                "disabled",
                "not loaded",
            ),
            (
                "duplicate-loaded",
                None,
                "enabled",
                "enabled",
                "unavailable",
                "duplicate",
            ),
            (
                "unexpected-gui-only",
                None,
                "disabled",
                "enabled",
                "enabled",
                "legacy GUI",
            ),
            (
                "audited-background-domain-mismatch",
                background_audit,
                "disabled",
                "enabled",
                "unavailable",
                "audited configuration",
            ),
            (
                "unrecognized-user",
                None,
                "unrecognized",
                "disabled",
                "unavailable",
                "without explicit",
            ),
            (
                "audited-gui-enabled",
                legacy_audit,
                "disabled",
                "enabled",
                "enabled",
                None,
            ),
            (
                "audited-gui-domain-mismatch",
                legacy_audit,
                "enabled",
                "disabled",
                "unavailable",
                "domain",
            ),
        )
        for (
            label,
            audit,
            user_state,
            gui_state,
            expected_classification,
            reason,
        ) in cases:
            with self.subTest(case=label):
                results = self.launchd_query_matrix(user_state, gui_state)
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=results,
                    ) as run,
                    mock.patch.object(
                        MODULE,
                        "_revalidate_scheduler_status_audit",
                    ),
                ):
                    query = MODULE._scheduler_daemon_enabled(
                        MODULE.SchedulerPaths(platform="macos"),
                        config_audit=audit,
                    )

                self.assertEqual(
                    run.call_count,
                    2 * (1 + len(MODULE.LEGACY_LAUNCHD_LABELS)),
                )
                self.assertEqual(
                    [call.args[0][2] for call in run.call_args_list],
                    [
                        f"{domain}/{os.getuid()}/{label}"
                        for label in (
                            MODULE.LAUNCHD_LABEL,
                            *MODULE.LEGACY_LAUNCHD_LABELS,
                        )
                        for domain in (
                            MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
                            MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
                        )
                    ],
                )
                self.assertEqual(query.classification, expected_classification)
                if reason is not None:
                    self.assertIn(reason, query.reason or "")

    def test_macos_daemon_query_rejects_loaded_legacy_services(self) -> None:
        legacy_label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        absent_audit = MODULE.SchedulerConfigAudit(
            config=None,
            snapshots=(),
        )
        cases = (
            ("duplicate", None, "enabled", "disabled", "unavailable"),
            ("unbound", None, "disabled", "disabled", "unavailable"),
            ("orphan", absent_audit, "disabled", "disabled", "enabled"),
            ("mixed-orphan", absent_audit, "enabled", "disabled", "enabled"),
        )
        for (
            case,
            config_audit,
            canonical_user_state,
            canonical_gui_state,
            expected_classification,
        ) in cases:
            for legacy_domain in (
                MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN,
                MODULE.MACOS_LEGACY_GUI_LAUNCHD_DOMAIN,
            ):
                with self.subTest(case=case, legacy_domain=legacy_domain):
                    results = self.launchd_query_matrix(
                        canonical_user_state,
                        canonical_gui_state,
                        legacy_overrides={(legacy_label, legacy_domain): "enabled"},
                    )
                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=results,
                        ) as run,
                        mock.patch.object(
                            MODULE,
                            "_revalidate_scheduler_status_audit",
                        ),
                    ):
                        query = MODULE._scheduler_daemon_enabled(
                            MODULE.SchedulerPaths(platform="macos"),
                            config_audit=config_audit,
                        )

                    self.assertEqual(
                        run.call_count,
                        2 * (1 + len(MODULE.LEGACY_LAUNCHD_LABELS)),
                    )
                    self.assertEqual(
                        query.classification,
                        expected_classification,
                    )
                    self.assertIn("scheduler", query.reason or "")
                    self.assertIn(legacy_label, query.reason or "")
                    self.assertIn(legacy_domain, query.reason or "")

    def test_macos_legacy_only_daemon_is_reported_as_orphan_active(self) -> None:
        case_user_home = self.root / "legacy-orphan-status" / "home"
        case_user_home.mkdir(parents=True)
        case_home = case_user_home / ".codex"
        legacy_label = MODULE.LEGACY_LAUNCHD_LABELS[0]

        def query_launchd(
            args: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            domain, _uid, label = args[2].split("/", 2)
            state = (
                "enabled"
                if label == legacy_label
                and domain == MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN
                else "disabled"
            )
            return self.launchd_query_result(domain, state, label=label)

        with (
            mock.patch.object(MODULE.Path, "home", return_value=case_user_home),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=query_launchd,
            ),
            mock.patch.object(MODULE, "audit_active_skills", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report = MODULE.scheduler_report(case_home, "macos")
            _doctor_report, issues = MODULE.doctor(
                case_home,
                "macos",
                json_output=False,
            )
            paths = MODULE._scheduler_paths("macos", case_home)

        self.assertFalse(report.installed)
        self.assertTrue(report.enabled)
        self.assertEqual(report.failure_code, "scheduler-orphan-active")
        assert report.daemon_query is not None
        self.assertEqual(report.daemon_query.classification, "enabled")
        self.assertIn("scheduler orphan", report.daemon_query.reason or "")
        self.assertIn(
            "scheduler-orphan-active",
            {issue.code for issue in issues},
        )
        self.assertFalse(MODULE._scheduler_config_parent(paths).exists())

    def test_macos_daemon_query_rejects_mixed_absence_and_denial(self) -> None:
        for evidence, expected_classification, expected_reason in (
            (
                "Permission denied: Could not find service in domain",
                "unavailable",
                "denied",
            ),
            (
                "Input/output error: Could not find service in domain",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                f"in domain for user gui: {os.getuid()}",
                "disabled",
                "not loaded",
            ),
            (
                "Bad request.\n"
                'Could not find service "mismatched.label" '
                f"in domain for user gui: {os.getuid()}",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL.upper()}" '
                f"in domain for user gui: {os.getuid()}",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                f"in domain for user gui: {os.getuid() + 1}",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                f"in domain for user gui: {os.getuid()}\n"
                "additional diagnostic",
                "unavailable",
                "without explicit",
            ),
        ):
            with self.subTest(evidence=evidence):
                completed = subprocess.CompletedProcess(
                    ["launchctl", "print"],
                    113,
                    "",
                    evidence,
                )
                results = [
                    self.launchd_query_result("user", "disabled"),
                    completed,
                    *self.launchd_query_matrix("disabled", "disabled")[2:],
                ]
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=results,
                    ) as run,
                ):
                    query = MODULE._scheduler_daemon_enabled(
                        MODULE.SchedulerPaths(platform="macos")
                    )
                self.assertEqual(
                    run.call_count,
                    2 * (1 + len(MODULE.LEGACY_LAUNCHD_LABELS)),
                )
                self.assertEqual(
                    query.classification,
                    expected_classification,
                )
                self.assertIn(expected_reason, query.reason or "")

    def test_scheduler_report_and_doctor_detect_orphan_daemon(self) -> None:
        for platform_name, classification in (
            ("macos", "enabled"),
            ("linux", "active-disabled"),
            ("linux", "enabled-inactive"),
        ):
            with self.subTest(
                platform=platform_name,
                classification=classification,
            ):
                case_user_home = (
                    self.root
                    / f"orphan-status-{platform_name}-{classification}"
                    / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                query = MODULE.SchedulerDaemonQuery(
                    classification,
                    f"observed {classification}",
                )
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        return_value=query,
                    ) as daemon_query,
                    mock.patch.object(
                        MODULE,
                        "audit_active_skills",
                        return_value=[],
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    report = MODULE.scheduler_report(
                        case_home,
                        platform_name,
                    )
                    _doctor_report, issues = MODULE.doctor(
                        case_home,
                        platform_name,
                        json_output=False,
                    )

                self.assertGreaterEqual(daemon_query.call_count, 2)
                self.assertFalse(report.installed)
                self.assertTrue(report.enabled)
                self.assertEqual(report.failure_code, "scheduler-orphan-active")
                self.assertEqual(
                    report.daemon_query,
                    query,
                )
                issue_codes = {issue.code for issue in issues}
                self.assertIn("scheduler-not-installed", issue_codes)
                self.assertIn("scheduler-orphan-active", issue_codes)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                self.assertFalse(MODULE._scheduler_config_parent(paths).exists())
                self.assertFalse(case_home.exists())

    def test_scheduler_status_binds_missing_parent_activation_absence(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-activation-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                marker = MODULE._scheduler_activation_transaction_path(paths)

                def begin_activation_during_query(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_bytes(
                        MODULE._scheduler_activation_transaction_payload(
                            case_home,
                            paths,
                        )
                    )
                    marker.chmod(0o600)
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "enabled\n",
                        "",
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=begin_activation_during_query,
                    ),
                ):
                    report = MODULE.scheduler_report(
                        case_home,
                        platform_name,
                    )

                self.assertTrue(marker.is_file())
                self.assertFalse(report.installed)
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )

    def test_scheduler_status_binds_missing_parent_uninstall_absence(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-uninstall-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                marker = MODULE._scheduler_uninstall_transaction_path(paths)

                def begin_uninstall_during_query(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_bytes(
                        MODULE._scheduler_uninstall_transaction_payload(
                            case_home,
                            paths,
                            disable=True,
                        )
                    )
                    marker.chmod(0o600)
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "enabled\n",
                        "",
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=begin_uninstall_during_query,
                    ),
                ):
                    report = MODULE.scheduler_report(
                        case_home,
                        platform_name,
                    )

                self.assertTrue(marker.is_file())
                self.assertFalse(report.installed)
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-uninstall-incomplete",
                )
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )

    def test_linux_status_requires_enabled_and_active_timer(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        query_results = (
            subprocess.CompletedProcess(
                ["systemctl", "is-enabled"],
                0,
                "enabled\n",
                "",
            ),
            subprocess.CompletedProcess(
                ["systemctl", "is-active"],
                3,
                "failed\n",
                "",
            ),
        )
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=query_results,
            ) as run,
            mock.patch.object(
                MODULE,
                "_stable_scheduler_runner_matches",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            [call.args[0][2] for call in run.call_args_list],
            ["is-enabled", "is-active"],
        )
        self.assertFalse(report.enabled)
        self.assertEqual(
            report.failure_code,
            "scheduler-daemon-disabled",
        )
        assert report.daemon_query is not None
        self.assertEqual(
            report.daemon_query.classification,
            "enabled-inactive",
        )
        self.assertIn(
            "enabled but not active (state failed)",
            report.daemon_query.reason or "",
        )

    def test_scheduler_report_and_doctor_preserve_runtime_and_daemon_failures(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        runtime_state = {
            "version": 2,
            "last_attempt": "2026-07-24T12:00:00+00:00",
            "last_success": None,
            "success": False,
            "failure_reason": "scheduled sync failed",
            "failure_code": None,
            "release_trees": {},
            "mode": "public",
            "repo": "owner/public-sync",
            "base_repo": "owner/public-sync",
            "owner": MODULE.PUBLIC_OWNER,
        }
        enablement = subprocess.CompletedProcess(
            ["systemctl"],
            1,
            "disabled\n",
            "",
        )
        activity = subprocess.CompletedProcess(
            ["systemctl"],
            3,
            "inactive\n",
            "",
        )
        with (
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
            mock.patch.object(
                MODULE,
                "_stable_scheduler_runner_matches",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=(
                    enablement,
                    activity,
                    enablement,
                    activity,
                ),
            ),
            mock.patch.object(
                MODULE,
                "audit_active_skills",
                return_value=[],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report = MODULE.scheduler_report(self.home, "linux")
            _doctor_report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=False,
            )

        self.assertEqual(report.failure_reason, "scheduled sync failed")
        self.assertIsNone(report.failure_code)
        self.assertFalse(report.enabled)
        assert report.daemon_query is not None
        self.assertEqual(report.daemon_query.classification, "disabled")
        self.assertIn(
            (None, "scheduled sync failed"),
            report.failures,
        )
        self.assertIn(
            (
                "scheduler-daemon-disabled",
                "systemd reports scheduler unit state disabled",
            ),
            report.failures,
        )
        issue_codes = {issue.code for issue in issues}
        self.assertIn("scheduler-failure", issue_codes)
        self.assertIn("scheduler-daemon-disabled", issue_codes)

    def test_scheduler_status_binds_config_across_native_query(self) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = self.root / f"status-{platform_name}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                    target = (
                        paths.launchd_plist
                        if platform_name == "macos"
                        else paths.systemd_service
                    )
                    assert target is not None
                    original = target.read_text(encoding="utf-8")

                    def mutate_during_status(
                        args: list[str],
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[str]:
                        target.write_text(
                            original.replace(
                                "owner/public-sync",
                                "owner/changed-sync",
                            ),
                            encoding="utf-8",
                        )
                        target.chmod(0o600)
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            "enabled\n",
                            "",
                        )

                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=mutate_during_status,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        ),
                    ):
                        report = MODULE.scheduler_report(
                            case_home,
                            platform_name,
                        )

                self.assertEqual(report.failure_code, "scheduler-config-drift")
                self.assertIsNone(report.enabled)
                self.assertIn("content changed", report.failure_reason or "")
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )
                self.assertIn(
                    "scheduler-config-drift",
                    {code for code, _reason in report.failures},
                )
                self.assertIn(
                    "scheduler-daemon-unavailable",
                    {code for code, _reason in report.failures},
                )

    def test_scheduler_status_binds_activation_absence_across_native_query(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"activation-status-{platform_name}" / "home"
                )
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                    marker = MODULE._scheduler_activation_transaction_path(paths)
                    self.assertFalse(marker.exists())

                    def begin_activation_during_status(
                        args: list[str],
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[str]:
                        marker.write_bytes(
                            MODULE._scheduler_activation_transaction_payload(
                                case_home,
                                paths,
                            )
                        )
                        marker.chmod(0o600)
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            "enabled\n",
                            "",
                        )

                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=begin_activation_during_status,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        ),
                    ):
                        report = MODULE.scheduler_report(
                            case_home,
                            platform_name,
                        )

                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertIsNone(report.enabled)
                self.assertIn("appeared", report.failure_reason or "")
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )
                failure_codes = {code for code, _reason in report.failures}
                self.assertIn(
                    "scheduler-activation-incomplete",
                    failure_codes,
                )
                self.assertNotIn(
                    "scheduler-daemon-unavailable",
                    failure_codes,
                )

    def test_scheduler_status_allows_mtime_churn_and_reports_unavailable(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    "owner/public-sync",
                    17,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_timer
                )
                assert target is not None

                def touch_during_status(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    metadata = target.stat()
                    os.utime(
                        target,
                        ns=(
                            metadata.st_atime_ns,
                            metadata.st_mtime_ns + 1_000_000,
                        ),
                    )
                    if platform_name == "macos" and len(args) > 2:
                        domain, _uid, label = args[2].split("/", 2)
                        state = (
                            "enabled"
                            if domain == MODULE.MACOS_BACKGROUND_LAUNCHD_DOMAIN
                            and label == MODULE.LAUNCHD_LABEL
                            else "disabled"
                        )
                        return self.launchd_query_result(
                            domain,
                            state,
                            label=label,
                        )
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        (
                            "active\n"
                            if len(args) > 2 and args[2] == "is-active"
                            else "enabled\n"
                        ),
                        "",
                    )

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_current_releases_for_scheduler",
                            return_value=(),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_scheduler_release_integrity_issues",
                            return_value=(),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_quarantine_batch_count",
                            return_value=0,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=touch_during_status,
                        )
                    )
                    healthy = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertTrue(healthy.enabled)
                self.assertIsNone(healthy.failure_code)

                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_stable_scheduler_runner_matches",
                        return_value=True,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_current_releases_for_scheduler",
                        return_value=(),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_release_integrity_issues",
                        return_value=(),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_quarantine_batch_count",
                        return_value=0,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=OSError("status unavailable"),
                    ),
                ):
                    unavailable = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertIsNone(unavailable.enabled)
                self.assertEqual(
                    unavailable.failure_code,
                    "scheduler-daemon-unavailable",
                )
                assert unavailable.daemon_query is not None
                self.assertEqual(
                    unavailable.daemon_query.classification,
                    "unavailable",
                )

    def test_linux_install_binds_semantically_audited_pair(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "linux")
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        original_timer = paths.systemd_timer.read_bytes()
        real_audit = MODULE._audit_scheduler_config
        replacement = b"user edited systemd service\n"

        def audit_then_edit(
            selected_paths: MODULE.SchedulerPaths,
        ) -> MODULE.SchedulerConfigAudit:
            audit = real_audit(selected_paths)
            paths.systemd_service.write_bytes(replacement)
            paths.systemd_service.chmod(0o600)
            return audit

        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                side_effect=audit_then_edit,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")

        self.assertEqual(paths.systemd_service.read_bytes(), replacement)
        self.assertEqual(paths.systemd_timer.read_bytes(), original_timer)
        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())

    def test_scheduler_exchange_never_restores_a_replaced_displaced_path(
        self,
    ) -> None:
        paths = (
            self.user_home
            / "Library"
            / "LaunchAgents"
            / "com.openai.codex-personal-sync.plist",
            self.user_home
            / ".config"
            / "systemd"
            / "user"
            / "codex-personal-sync.service",
        )
        for config_path in paths:
            with self.subTest(path=config_path):
                config_path.parent.mkdir(parents=True, exist_ok=True)
                original = f"original:{config_path.name}\n".encode()
                replacement = f"replacement:{config_path.name}\n".encode()
                attacker = f"attacker:{config_path.name}\n".encode()
                config_path.write_bytes(original)
                config_path.chmod(0o600)
                original_identity = (
                    config_path.stat().st_dev,
                    config_path.stat().st_ino,
                )
                expected = MODULE._scheduler_config_snapshot(config_path)
                real_exchange = MODULE._rename_exchange_at
                exchange_count = 0

                def exchange_then_replace_displaced(
                    first_parent_fd: int,
                    first_name: str,
                    second_parent_fd: int,
                    second_name: str,
                ) -> None:
                    nonlocal exchange_count
                    real_exchange(
                        first_parent_fd,
                        first_name,
                        second_parent_fd,
                        second_name,
                    )
                    exchange_count += 1
                    if exchange_count == 1:
                        displaced_path = config_path.with_name(first_name)
                        displaced_path.unlink()
                        displaced_path.write_bytes(attacker)
                        displaced_path.chmod(0o600)

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_exchange_at",
                        side_effect=exchange_then_replace_displaced,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "trusted staged config remains live.*exact original "
                        "is preserved",
                    ),
                ):
                    MODULE._atomic_write_scheduler_config(
                        config_path,
                        replacement,
                        expected_snapshot=expected,
                    )

                self.assertEqual(exchange_count, 1)
                self.assertEqual(config_path.read_bytes(), replacement)
                self.assertNotEqual(config_path.read_bytes(), attacker)
                recovery_paths = list(
                    config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*.original"
                    )
                )
                self.assertEqual(len(recovery_paths), 1)
                self.assertEqual(recovery_paths[0].read_bytes(), original)
                self.assertEqual(
                    (
                        recovery_paths[0].stat().st_dev,
                        recovery_paths[0].stat().st_ino,
                    ),
                    original_identity,
                )
                displaced_paths = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced_paths), 1)
                self.assertEqual(displaced_paths[0].read_bytes(), attacker)

    def test_scheduler_cleanup_removes_recovery_before_displaced_preimage(
        self,
    ) -> None:
        config_path = (
            self.user_home
            / "Library"
            / "LaunchAgents"
            / "com.openai.codex-personal-sync.plist"
        )
        config_path.parent.mkdir(parents=True)
        original = b"original scheduler config\n"
        replacement = b"replacement scheduler config\n"
        config_path.write_bytes(original)
        config_path.chmod(0o600)
        expected = MODULE._scheduler_config_snapshot(config_path)
        cleanup_labels: list[str] = []
        real_cleanup = MODULE._isolate_and_delete_pending_cleanup_file

        def observe_cleanup(
            home: Path,
            path: Path,
            parent_fd: int,
            snapshot: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            cleanup_labels.append(label)
            if "recovery evidence" in label:
                displaced = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced), 1)
                self.assertEqual(displaced[0].read_bytes(), original)
            real_cleanup(
                home,
                path,
                parent_fd,
                snapshot,
                label=label,
            )

        with mock.patch.object(
            MODULE,
            "_isolate_and_delete_pending_cleanup_file",
            side_effect=observe_cleanup,
        ):
            installed = MODULE._atomic_write_scheduler_config(
                config_path,
                replacement,
                expected_snapshot=expected,
            )

        self.assertEqual(installed.payload, replacement)
        self.assertEqual(config_path.read_bytes(), replacement)
        scheduler_cleanup_labels = [
            label for label in cleanup_labels if label.startswith("scheduler config ")
        ]
        self.assertEqual(
            scheduler_cleanup_labels,
            [
                f"scheduler config recovery evidence {config_path}",
                f"scheduler config backup {config_path}",
            ],
        )

    def test_scheduler_recovery_cleanup_races_preserve_displaced_preimage(
        self,
    ) -> None:
        for race in ("unlink", "replace"):
            with self.subTest(race=race):
                config_path = self.user_home / race / "codex-personal-sync.service"
                config_path.parent.mkdir(parents=True)
                original = f"original:{race}\n".encode()
                replacement = f"replacement:{race}\n".encode()
                config_path.write_bytes(original)
                config_path.chmod(0o600)
                expected = MODULE._scheduler_config_snapshot(config_path)
                real_cleanup = MODULE._isolate_and_delete_pending_cleanup_file
                injected = False

                def race_recovery_cleanup(
                    home: Path,
                    path: Path,
                    parent_fd: int,
                    snapshot: MODULE.ManagedStateFileSnapshot,
                    *,
                    label: str,
                ) -> None:
                    nonlocal injected
                    if "recovery evidence" in label and not injected:
                        injected = True
                        path.unlink()
                        if race == "replace":
                            # Preserve content and access while replacing the
                            # named object identity.
                            path.write_bytes(original)
                            path.chmod(0o600)
                    real_cleanup(
                        home,
                        path,
                        parent_fd,
                        snapshot,
                        label=label,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                        side_effect=race_recovery_cleanup,
                    ),
                    self.assertRaises(MODULE.SyncError),
                ):
                    MODULE._atomic_write_scheduler_config(
                        config_path,
                        replacement,
                        expected_snapshot=expected,
                    )

                self.assertTrue(injected)
                self.assertEqual(config_path.read_bytes(), replacement)
                displaced = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced), 1)
                self.assertEqual(displaced[0].read_bytes(), original)

    def test_scheduler_recovery_link_survives_parent_fsync_failure(self) -> None:
        config_path = (
            self.user_home
            / ".config"
            / "systemd"
            / "user"
            / "codex-personal-sync.service"
        )
        config_path.parent.mkdir(parents=True)
        original = b"original scheduler config\n"
        config_path.write_bytes(original)
        config_path.chmod(0o600)
        expected = MODULE._scheduler_config_snapshot(config_path)
        real_fsync = MODULE.os.fsync
        parent_fsyncs = 0

        def fail_recovery_parent_fsync(file_fd: int) -> None:
            nonlocal parent_fsyncs
            if stat.S_ISDIR(os.fstat(file_fd).st_mode):
                parent_fsyncs += 1
                if parent_fsyncs == 2:
                    raise OSError("injected recovery parent fsync failure")
            real_fsync(file_fd)

        with (
            mock.patch.object(
                MODULE.os,
                "fsync",
                side_effect=fail_recovery_parent_fsync,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "failed to publish scheduler config",
            ),
        ):
            MODULE._atomic_write_scheduler_config(
                config_path,
                b"replacement scheduler config\n",
                expected_snapshot=expected,
            )

        recovery_paths = list(
            config_path.parent.glob(
                f".{config_path.name}.personal-sync-write-*.original"
            )
        )
        self.assertEqual(len(recovery_paths), 1)
        self.assertEqual(recovery_paths[0].read_bytes(), original)
        self.assertEqual(config_path.read_bytes(), original)

    def test_matching_scheduler_revalidates_semantic_audit_before_daemon(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    "owner/current",
                    17,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                real_audit = MODULE._audit_scheduler_config
                replacement = f"user edited {platform_name} config\n".encode()

                def audit_then_edit(
                    selected_paths: MODULE.SchedulerPaths,
                ) -> MODULE.SchedulerConfigAudit:
                    audit = real_audit(selected_paths)
                    target.write_bytes(replacement)
                    target.chmod(0o600)
                    return audit

                with (
                    mock.patch.object(
                        MODULE,
                        "_audit_scheduler_config",
                        side_effect=audit_then_edit,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                    ) as run_native,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "changed after semantic audit",
                    ),
                ):
                    self.install_scheduler_quietly(
                        "owner/current",
                        None,
                        platform_name,
                        enable=True,
                    )

                run_native.assert_not_called()
                self.assertEqual(target.read_bytes(), replacement)

    def test_scheduler_after_snapshot_ignores_non_access_bearing_group(self) -> None:
        payload = b"service\n"
        wrong_group = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=payload,
            mode=0o600,
            file_identity=(1, 2),
            file_type=stat.S_IFREG,
            size=len(payload),
            uid=os.geteuid(),
            gid=os.getegid() + 1,
        )
        self.assertTrue(
            MODULE._scheduler_snapshot_matches_after_payload(
                wrong_group,
                payload,
            )
        )
        expected_group = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=payload,
            mode=0o600,
            file_identity=(1, 3),
            file_type=stat.S_IFREG,
            size=len(payload),
            uid=os.geteuid(),
            gid=os.getegid(),
        )
        self.assertTrue(
            MODULE._scheduler_snapshot_matches_after_payload(
                expected_group,
                payload,
            )
        )

    def test_scheduler_rollback_restores_before_group_in_setgid_parent(
        self,
    ) -> None:
        caller_groups = {os.getegid(), *os.getgroups()}
        alternate_groups = sorted(caller_groups - {os.getegid()})
        if not alternate_groups:
            self.skipTest("caller has no alternate group for setgid regression")
        parent_gid = alternate_groups[0]
        config_parent = self.home / "scheduler-group-rollback"
        config_parent.mkdir()
        try:
            os.chown(config_parent, -1, parent_gid)
            config_parent.chmod(0o2755)
        except PermissionError as error:
            self.skipTest(f"cannot prepare setgid regression directory: {error}")
        if config_parent.stat().st_gid != parent_gid:
            self.skipTest("filesystem did not preserve requested parent group")

        config = config_parent / "service.conf"
        config.write_bytes(b"after\n")
        config.chmod(0o600)
        current = MODULE._scheduler_config_snapshot(config)
        if current.gid != parent_gid:
            self.skipTest("filesystem did not inherit setgid parent group")
        desired_payload = b"before\n"
        desired = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=desired_payload,
            mode=0o640,
            file_identity=(1, 1),
            file_type=stat.S_IFREG,
            size=len(desired_payload),
            uid=os.geteuid(),
            gid=os.getegid(),
        )

        MODULE._restore_scheduler_config_snapshot(
            config,
            current=current,
            desired=desired,
        )

        restored = MODULE._scheduler_config_snapshot(config)
        self.assertTrue(MODULE._scheduler_file_logical_state_matches(restored, desired))

    def test_scheduler_report_requires_every_configured_current(self) -> None:
        public_config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "public.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        private_config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "private.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="private",
            repo="owner/private-sync",
            base_repo="owner/public-sync",
            owner="private",
        )

        cases = (
            (
                "public",
                public_config,
                lambda _home, _owner: None,
                MODULE.PUBLIC_OWNER,
            ),
            (
                "private",
                private_config,
                lambda _home, owner: (
                    PUBLIC_SHA if owner == MODULE.PUBLIC_OWNER else None
                ),
                "private",
            ),
        )
        for label, config, current_sha, expected_owner in cases:
            with (
                self.subTest(label=label),
                mock.patch.object(
                    MODULE,
                    "_audit_scheduler_config",
                    return_value=MODULE.SchedulerConfigAudit(
                        config=config,
                        snapshots=(),
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "_retain_scheduler_config_audit_bindings",
                    return_value=contextlib.nullcontext(()),
                ),
                mock.patch.object(
                    MODULE,
                    "_retain_launchd_activation_binding",
                    return_value=contextlib.nullcontext(
                        mock.sentinel.activation_binding
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "_read_scheduler_runtime_state",
                    return_value=None,
                ),
                mock.patch.object(
                    MODULE,
                    "_current_sha",
                    side_effect=current_sha,
                ),
                mock.patch.object(
                    MODULE,
                    "_quarantine_batch_count",
                    return_value=0,
                ),
                mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=True,
                ),
            ):
                report = MODULE.scheduler_report(self.home, "linux")

            self.assertEqual(report.failure_code, "current-release-missing")
            self.assertIn(expected_owner, report.failure_reason or "")
            self.assertEqual(report.current_releases, ())

    def test_quarantine_audit_preserves_prior_codeless_runtime_failure(
        self,
    ) -> None:
        config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "scheduler.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        runtime_state = {
            "version": 2,
            "last_attempt": "2026-07-23T09:00:00+00:00",
            "last_success": None,
            "success": False,
            "failure_reason": "legacy failure without a code",
            "failure_code": None,
            "release_trees": {},
            "mode": "public",
            "repo": "owner/public-sync",
            "base_repo": "owner/public-sync",
            "owner": MODULE.PUBLIC_OWNER,
        }
        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                return_value=MODULE.SchedulerConfigAudit(
                    config=config,
                    snapshots=(),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_retain_scheduler_config_audit_bindings",
                return_value=contextlib.nullcontext(()),
            ),
            mock.patch.object(
                MODULE,
                "_retain_launchd_activation_binding",
                return_value=contextlib.nullcontext(mock.sentinel.activation_binding),
            ),
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                side_effect=MODULE.SyncError("audit unavailable"),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                return_value=True,
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            report.failure_reason,
            "legacy failure without a code",
        )
        self.assertIsNone(report.failure_code)

    def test_runtime_v1_is_normalized_and_failure_code_is_persisted(
        self,
    ) -> None:
        previous_success = "2026-07-22T08:00:00+00:00"
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 1,
                "last_attempt": previous_success,
                "last_success": previous_success,
                "success": True,
                "failure_reason": None,
                "mode": "public",
                "repo": "owner/public-sync",
                "base_repo": "owner/public-sync",
                "owner": MODULE.PUBLIC_OWNER,
            },
        )
        normalized = MODULE._read_scheduler_runtime_state(self.home)
        assert normalized is not None
        self.assertEqual(normalized["version"], 2)
        self.assertIsNone(normalized["failure_code"])

        with (
            mock.patch.object(
                MODULE,
                "install_from_github",
                side_effect=MODULE.SyncError(
                    "existing release tree differs",
                    code="immutable-release-drift",
                ),
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/public-sync",
                mode="public",
                base_repo="owner/ignored",
                owner="private",
            )
        failed = MODULE._read_scheduler_runtime_state(self.home)
        assert failed is not None
        self.assertEqual(failed["version"], 2)
        self.assertEqual(failed["failure_code"], "immutable-release-drift")
        self.assertEqual(failed["last_success"], previous_success)

    def test_doctor_reports_quarantine_saturation_without_mutation(self) -> None:
        quarantine = self.home / "personal-sync" / MODULE.QUARANTINE_RELATIVE_PATH
        quarantine.mkdir(parents=True)
        for index in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES):
            prefix = MODULE.PENDING_CLEANUP_ISOLATED_BATCH_PREFIX if index % 2 else ""
            (quarantine / f"{prefix}20260723T000000Z-{index + 1}-{index + 1}").mkdir()
        before = snapshot_tree(quarantine)

        with contextlib.redirect_stdout(io.StringIO()):
            report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        self.assertEqual(
            report.quarantine_batches,
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES,
        )
        saturated = [issue for issue in issues if issue.code == "quarantine-saturated"]
        self.assertEqual(len(saturated), 1)
        self.assertIn(
            f">= {MODULE.MAX_RETAINED_QUARANTINE_BATCHES}",
            saturated[0].detail,
        )
        self.assertEqual(snapshot_tree(quarantine), before)

    def test_doctor_reports_mirror_quarantine_recovery_without_mutation(
        self,
    ) -> None:
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        for index in range(2):
            evidence = quarantine / f"transient-evidence-{index}"
            evidence.write_bytes(f"evidence {index}\n".encode())
            evidence.chmod(0o600)

        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.0123456789abcdef0123456789abcdef"
        )
        owner_name = f"{private_name}.owner.json"
        owner_nonce = "fedcba9876543210fedcba9876543210"
        expected_private_identity = [123, 456, stat.S_IFDIR]
        owner_path = tool_root / owner_name
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": owner_nonce,
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": expected_private_identity,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)
        real_bind = MODULE._bind_mirror_primary_control_parent
        observed_bind_paths = []

        def reject_default_host_parent(spec):
            candidate = Path(os.path.abspath(spec.parent_path))
            self.assertNotEqual(
                candidate,
                self.host_mirror_private_control_parent,
            )
            observed_bind_paths.append(candidate)
            return real_bind(spec)

        doctor_output = io.StringIO()
        strict_output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "MIRROR_DURABLE_QUARANTINE_ENTRY_LIMIT",
                2,
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_primary_control_parent",
                side_effect=reject_default_host_parent,
            ),
        ):
            with contextlib.redirect_stdout(doctor_output):
                report, issues = MODULE.doctor(
                    self.home,
                    "linux",
                    json_output=True,
                )
            with contextlib.redirect_stdout(strict_output):
                strict_status = MODULE.main(
                    [
                        "status-scheduler",
                        "--home",
                        str(self.home),
                        "--platform",
                        "linux",
                        "--json",
                        "--strict",
                    ]
                )

        self.assertEqual(strict_status, 1)
        audit = report.mirror_quarantine
        assert audit is not None
        self.assertEqual(audit.classification, "saturated")
        self.assertEqual(audit.entry_count, 2)
        self.assertEqual(audit.entry_limit, 2)
        self.assertFalse(audit.count_is_lower_bound)
        self.assertEqual(
            audit.segment_identity,
            MODULE._mirror_object_identity(quarantine.stat()),
        )
        self.assertEqual(len(audit.owner_records), 1)
        owner_record = audit.owner_records[0]
        self.assertEqual(owner_record.name, owner_name)
        self.assertEqual(owner_record.state, "stale")
        self.assertEqual(owner_record.owner_nonce, owner_nonce)
        self.assertEqual(owner_record.phase, "cleanup")
        self.assertEqual(owner_record.private_name, private_name)
        self.assertEqual(
            owner_record.expected_private_identity,
            tuple(expected_private_identity),
        )
        self.assertEqual(owner_record.private_state, "missing")
        self.assertIsNotNone(owner_record.sha256)
        self.assertIn(
            "mirror-quarantine-saturated",
            {issue.code for issue in issues},
        )
        payload = json.loads(doctor_output.getvalue())
        self.assertEqual(
            payload["scheduler"]["mirror_quarantine"]["classification"],
            "saturated",
        )
        self.assertEqual(
            payload["scheduler"]["mirror_quarantine"]["owner_records"][0][
                "owner_nonce"
            ],
            owner_nonce,
        )
        self.assertEqual(
            observed_bind_paths,
            [
                self.mirror_private_control_parent,
                self.mirror_private_control_parent,
                self.mirror_private_control_parent,
                self.mirror_private_control_parent,
            ],
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_scheduler_status_reports_mirror_quarantine_audit_inconclusive(
        self,
    ) -> None:
        (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        ).mkdir(mode=0o700)
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        quarantine.chmod(0o755)
        before = snapshot_tree(self.mirror_private_control_parent)

        with contextlib.redirect_stdout(io.StringIO()):
            report = MODULE.status_scheduler(
                self.home,
                "linux",
                json_output=True,
            )

        audit = report.mirror_quarantine
        assert audit is not None
        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn(
            "must be mode 0700",
            audit.detail,
        )
        self.assertIn(
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
            {code for code, _detail in report.failures},
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_registry_skips_foreign_legacy_without_opening_it(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-shared-parent"
        shared_parent.mkdir(mode=0o700)
        shared_parent.chmod(0o1777)
        preclaimed = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        preclaimed.mkdir(mode=0o700)
        preclaimed.chmod(0o000)
        before = preclaimed.lstat()
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_metadata = MODULE._mirror_legacy_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory
        legacy_child_opens = []
        legacy_metadata_calls: dict[str, int] = {}

        def classify_preclaim_as_foreign(parent_fd, name, root_id):
            legacy_metadata_calls[name] = legacy_metadata_calls.get(name, 0) + 1
            observed = real_metadata(parent_fd, name, root_id)
            if name == MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME and observed is not None:
                identity, access_policy = observed
                return identity, (
                    access_policy[0],
                    os.geteuid() + 1,
                    access_policy[2],
                )
            return observed

        def reject_legacy_child_open(parent_fd, parent_path, name, label):
            if parent_path == shared_parent:
                legacy_child_opens.append(name)
                self.fail(f"foreign legacy child was opened: {name}")
            return real_bind_child(parent_fd, parent_path, name, label)

        observed_after = None
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MODULE,
                    "_mirror_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MODULE,
                    "_mirror_legacy_child_metadata",
                    side_effect=classify_preclaim_as_foreign,
                ),
                mock.patch.object(
                    MODULE,
                    "_bind_mirror_audit_child_directory",
                    side_effect=reject_legacy_child_open,
                ),
            ):
                audit = MODULE._mirror_quarantine_audit()
            observed_after = preclaimed.lstat()
        finally:
            preclaimed.chmod(0o700)

        self.assertEqual(audit.classification, "absent")
        self.assertEqual(
            [root.root_id for root in audit.root_audits],
            [primary_spec.root_id, legacy_spec.root_id],
        )
        self.assertEqual(audit.root_audits[1].classification, "foreign-unrelated")
        self.assertEqual(
            legacy_metadata_calls,
            {
                MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME: 3,
                MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME: 3,
            },
        )
        payload = MODULE._mirror_quarantine_payload(audit)
        assert payload is not None
        self.assertEqual(payload["roots"][1]["root_id"], legacy_spec.root_id)
        self.assertEqual(legacy_child_opens, [])
        assert observed_after is not None
        self.assertEqual(
            (
                observed_after.st_dev,
                observed_after.st_ino,
                stat.S_IMODE(observed_after.st_mode),
            ),
            (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode)),
        )

    def test_mirror_quarantine_terminal_revalidation_covers_every_root(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_specs = []
        for index in range(2):
            parent = self.root / f"terminal-legacy-{index}"
            parent.mkdir(mode=0o700)
            legacy_specs.append(
                MODULE.MirrorPrivateControlRootSpec(
                    root_id=f"terminal-legacy-{index}",
                    parent_path=parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
            )
        early_tool = legacy_specs[0].parent_path / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        early_tool.mkdir(mode=0o700)
        real_root_audit = MODULE._mirror_quarantine_root_audit
        real_metadata = MODULE._mirror_legacy_child_metadata
        metadata_calls: dict[tuple[str, str], int] = {}
        mutated = False

        def count_metadata(parent_fd, name, root_id):
            key = (root_id, name)
            metadata_calls[key] = metadata_calls.get(key, 0) + 1
            return real_metadata(parent_fd, name, root_id)

        def mutate_early_root_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            nonlocal mutated
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == legacy_specs[-1].root_id:
                early_tool.chmod(0o755)
                mutated = True
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, *legacy_specs),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_child_metadata",
                side_effect=count_metadata,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=mutate_early_root_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertTrue(mutated)
        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
        )
        self.assertIn(
            "terminal mirror quarantine registry revalidation covered every root",
            audit.detail,
        )
        for spec in (primary_spec, *legacy_specs):
            self.assertIn(spec.root_id, audit.detail)
        self.assertEqual(audit.root_audits[1].classification, "inconclusive")
        self.assertGreaterEqual(
            metadata_calls[
                (
                    legacy_specs[-1].root_id,
                    MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME,
                )
            ],
            3,
        )

    def test_mirror_quarantine_terminal_revalidates_absent_parent_anchor(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        absent_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-absent-legacy",
            parent_path=self.root / "terminal-absent-legacy",
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        last_parent = self.root / "terminal-last-legacy"
        last_parent.mkdir(mode=0o700)
        last_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-last-legacy",
            parent_path=last_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_root_audit = MODULE._mirror_quarantine_root_audit

        def create_absent_parent_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == last_spec.root_id:
                absent_spec.parent_path.mkdir(mode=0o700)
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, absent_spec, last_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=create_absent_parent_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
        )
        self.assertIn(absent_spec.root_id, audit.detail)
        self.assertIn("is no longer absent", audit.detail)

    def test_mirror_quarantine_terminal_revalidates_primary_absence_anchor(
        self,
    ) -> None:
        account_home = self.root / "terminal-primary-home"
        account_home.mkdir(mode=0o700)
        primary_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-absent-primary",
            parent_path=(account_home / MODULE.MIRROR_PRIVATE_CONTROL_NAMESPACE_NAME),
            allocate=True,
            account_home=account_home,
            shared_parent=False,
        )
        last_parent = self.root / "terminal-primary-last-legacy"
        last_parent.mkdir(mode=0o700)
        last_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-primary-last-legacy",
            parent_path=last_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_root_audit = MODULE._mirror_quarantine_root_audit

        def create_primary_parent_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == last_spec.root_id:
                primary_spec.parent_path.mkdir(mode=0o700)
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, last_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=create_primary_parent_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
        )
        self.assertIn(primary_spec.root_id, audit.detail)
        self.assertIn("is no longer absent", audit.detail)

    def test_mirror_quarantine_terminal_duplicate_revalidates_only_parent(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        shared_parent = self.root / "terminal-duplicate-parent"
        shared_parent.mkdir(mode=0o700)
        first_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-original-legacy",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        duplicate_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-duplicate-legacy",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_metadata = MODULE._mirror_legacy_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory
        metadata_root_ids: list[str] = []
        duplicate_child_opens: list[str] = []

        def record_metadata_root(parent_fd, name, root_id):
            metadata_root_ids.append(root_id)
            return real_metadata(parent_fd, name, root_id)

        def reject_duplicate_child_open(parent_fd, parent_path, name, label):
            if parent_path == shared_parent:
                duplicate_child_opens.append(name)
                self.fail(f"duplicate root child was opened: {name}")
            return real_bind_child(parent_fd, parent_path, name, label)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, first_spec, duplicate_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_child_metadata",
                side_effect=record_metadata_root,
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_audit_child_directory",
                side_effect=reject_duplicate_child_open,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.root_audits[2].classification, "duplicate")
        self.assertNotIn(duplicate_spec.root_id, metadata_root_ids)
        self.assertEqual(duplicate_child_opens, [])

    def test_mirror_quarantine_foreign_topology_is_metadata_only(self) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        real_metadata = MODULE._mirror_legacy_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory
        for topology_case in ("different-device", "inverted-containment"):
            with self.subTest(topology_case=topology_case):
                shared_parent = self.root / f"foreign-topology-{topology_case}"
                shared_parent.mkdir(mode=0o700)
                tool = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
                quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
                tool.mkdir(mode=0o700)
                quarantine.mkdir(mode=0o700)
                legacy_spec = MODULE.MirrorPrivateControlRootSpec(
                    root_id=f"foreign-topology-{topology_case}",
                    parent_path=shared_parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
                ancestor_identity = MODULE._mirror_object_identity(
                    shared_parent.parent.stat()
                )
                child_opens: list[str] = []

                def foreign_topology_metadata(parent_fd, name, root_id):
                    observed = real_metadata(parent_fd, name, root_id)
                    assert observed is not None
                    identity, access_policy = observed
                    if (
                        topology_case == "different-device"
                        and name == MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
                    ):
                        identity = (identity[0] + 1, identity[1], identity[2])
                    elif (
                        topology_case == "inverted-containment"
                        and name == MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
                    ):
                        identity = ancestor_identity
                    return identity, (
                        access_policy[0],
                        os.geteuid() + 1,
                        access_policy[2],
                    )

                def reject_foreign_child_open(parent_fd, parent_path, name, label):
                    if parent_path == shared_parent:
                        child_opens.append(name)
                        self.fail(f"foreign topology child was opened: {name}")
                    return real_bind_child(parent_fd, parent_path, name, label)

                with (
                    mock.patch.object(
                        MODULE,
                        "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                        (primary_spec, legacy_spec),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_mirror_legacy_shared_parent_policy_is_valid",
                        return_value=True,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_mirror_legacy_child_metadata",
                        side_effect=foreign_topology_metadata,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_bind_mirror_audit_child_directory",
                        side_effect=reject_foreign_child_open,
                    ),
                ):
                    audit = MODULE._mirror_quarantine_audit()

                self.assertEqual(audit.classification, "absent")
                self.assertEqual(
                    audit.root_audits[1].classification,
                    "foreign-unrelated",
                )
                self.assertEqual(
                    MODULE._mirror_private_control_preallocation_decision(
                        ("foreign-unrelated",)
                    ),
                    (True, None),
                )
                self.assertEqual(child_opens, [])

    def test_mirror_quarantine_bound_topology_requires_child_below_parent(
        self,
    ) -> None:
        parent = self.root / "bound-topology-parent"
        child = parent / "bound-topology-child"
        child.mkdir(parents=True, mode=0o700)
        parent_fd = os.open(parent, MODULE._source_directory_flags())
        child_fd = os.open(child, MODULE._source_directory_flags())
        try:
            parent_identity = MODULE._mirror_object_identity(os.fstat(parent_fd))
            child_identity = MODULE._mirror_object_identity(os.fstat(child_fd))
            MODULE._validate_mirror_private_control_bound_topology(
                root_id="valid-topology",
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                tool_fd=child_fd,
                tool_identity=child_identity,
            )
            with self.assertRaisesRegex(MODULE.SyncError, "strict descendant"):
                MODULE._validate_mirror_private_control_bound_topology(
                    root_id="inverted-topology",
                    parent_fd=child_fd,
                    parent_identity=child_identity,
                    tool_fd=parent_fd,
                    tool_identity=parent_identity,
                )
        finally:
            os.close(child_fd)
            os.close(parent_fd)

    def test_mirror_quarantine_bound_topology_requires_same_filesystem(
        self,
    ) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_mirror_directory_is_at_or_below",
                side_effect=(True, False, True, False),
            ),
            mock.patch.object(
                MODULE.os,
                "fstat",
                side_effect=(mock.Mock(st_dev=1), mock.Mock(st_dev=2)),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "same filesystem"),
        ):
            MODULE._validate_mirror_private_control_bound_topology(
                root_id="same-uid-legacy",
                parent_fd=10,
                parent_identity=(1, 10, stat.S_IFDIR),
                tool_fd=11,
                tool_identity=(1, 11, stat.S_IFDIR),
                quarantine_fd=12,
                quarantine_identity=(2, 12, stat.S_IFDIR),
            )

    def test_mirror_quarantine_same_uid_legacy_topology_fails_before_scan(
        self,
    ) -> None:
        shared_parent = self.root / "same-uid-topology-parent"
        shared_parent.mkdir(mode=0o700)
        (shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME).mkdir(mode=0o700)
        (shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME).mkdir(mode=0o700)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="same-uid-topology-legacy",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_validate = MODULE._validate_mirror_private_control_bound_topology

        def reject_same_uid_combined_topology(**kwargs):
            if (
                kwargs["root_id"] == legacy_spec.root_id
                and kwargs.get("quarantine_fd", -1) >= 0
            ):
                raise MODULE.SyncError("synthetic same-uid legacy topology failure")
            return real_validate(**kwargs)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_validate_mirror_private_control_bound_topology",
                side_effect=reject_same_uid_combined_topology,
            ),
            mock.patch.object(MODULE, "_bounded_mirror_directory_names") as inventory,
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn("synthetic same-uid legacy topology failure", audit.detail)
        inventory.assert_not_called()

    def test_mirror_quarantine_does_not_scan_before_full_topology_validation(
        self,
    ) -> None:
        tool = self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        real_validate = MODULE._validate_mirror_private_control_bound_topology

        def reject_combined_topology(**kwargs):
            if kwargs.get("quarantine_fd", -1) >= 0:
                raise MODULE.SyncError("synthetic fixed-root overlap")
            return real_validate(**kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_validate_mirror_private_control_bound_topology",
                side_effect=reject_combined_topology,
            ),
            mock.patch.object(MODULE, "_bounded_mirror_directory_names") as inventory,
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn("synthetic fixed-root overlap", audit.detail)
        inventory.assert_not_called()

    def test_mirror_quarantine_terminal_allows_benign_child_entry_churn(
        self,
    ) -> None:
        tool = self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        last_parent = self.root / "benign-churn-last-root"
        last_parent.mkdir(mode=0o700)
        last_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="benign-churn-last-root",
            parent_path=last_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_root_audit = MODULE._mirror_quarantine_root_audit

        def add_benign_entry_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == last_spec.root_id:
                (tool / "benign-child-entry").mkdir(mode=0o700)
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, last_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=add_benign_entry_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertNotEqual(audit.classification, "inconclusive", audit)

    def test_mirror_quarantine_registry_reports_same_uid_legacy_pending(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-current-parent"
        shared_parent.mkdir(mode=0o700)
        shared_parent.chmod(0o1777)
        (shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME).mkdir(mode=0o700)
        quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        quarantine.mkdir(mode=0o700)
        evidence = quarantine / "retained-evidence"
        evidence.write_bytes(b"retained\n")
        evidence.chmod(0o600)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        before = snapshot_tree(shared_parent)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        self.assertEqual(audit.root_id, legacy_spec.root_id)
        self.assertEqual(audit.entry_count, 1)
        self.assertIn("original root", audit.detail)
        self.assertEqual(snapshot_tree(shared_parent), before)

    @contextlib.contextmanager
    def retained_recovery_scope(self, name: str):
        shared_parent = self.root / f"legacy-retained-parent-{name}"
        shared_parent.mkdir(mode=0o700)
        tool = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        evidence = quarantine / "retained-evidence"
        evidence.write_bytes(b"retained\n")
        evidence.chmod(0o600)
        primary_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_PRIMARY_ROOT_ID,
            parent_path=self.mirror_private_control_parent,
            allocate=True,
            account_home=self.root,
            shared_parent=False,
        )
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        plan_path = self.root / f"{name}-recovery-plan.json"
        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            yield {
                "evidence": evidence,
                "plan_path": plan_path,
                "quarantine": quarantine,
                "shared_parent": shared_parent,
                "tool": tool,
            }

    def assert_retained_recovery_unpublished(self) -> None:
        self.assertFalse(
            (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            ).exists()
        )
        self.assertFalse(
            (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            ).exists()
        )

    def test_retained_recovery_rejects_plan_and_evidence_drift(self) -> None:
        with self.retained_recovery_scope("tampered-plan") as fixture:
            plan_path = fixture["plan_path"]
            assert isinstance(plan_path, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            tampered = json.loads(plan_path.read_text(encoding="utf-8"))
            evidence_entry = next(
                entry
                for entry in tampered["inventory"]["entries"]
                if entry["locator"]["path"] == "retained-evidence"
            )
            evidence_entry["sha256"] = "0" * 64
            tampered["plan_digest"] = MODULE._pc_recovery_digest(
                MODULE._pc_recovery_protected_plan(tampered)
            )
            plan_path.write_bytes(
                MODULE._pc_recovery_json_bytes(tampered, pretty=True)
            )
            plan_path.chmod(0o600)
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "plan no longer matches retained evidence",
            ):
                MODULE.execute_private_control_recovery(
                    MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )
            self.assert_retained_recovery_unpublished()

        with self.retained_recovery_scope("changed-evidence") as fixture:
            plan_path = fixture["plan_path"]
            evidence = fixture["evidence"]
            assert isinstance(plan_path, Path)
            assert isinstance(evidence, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            original_identity = MODULE._pc_recovery_identity(
                os.stat(evidence, follow_symlinks=False)
            )
            original_access = MODULE._pc_recovery_access(
                os.stat(evidence, follow_symlinks=False)
            )
            evidence.write_bytes(b"changed!\n")
            changed_metadata = os.stat(evidence, follow_symlinks=False)
            self.assertEqual(
                MODULE._pc_recovery_identity(changed_metadata),
                original_identity,
            )
            self.assertEqual(
                MODULE._pc_recovery_access(changed_metadata),
                original_access,
            )
            self.assertEqual(changed_metadata.st_size, len(b"retained\n"))
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "plan no longer matches retained evidence",
            ):
                MODULE.execute_private_control_recovery(
                    MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )
            self.assert_retained_recovery_unpublished()

        with self.retained_recovery_scope("replaced-evidence") as fixture:
            plan_path = fixture["plan_path"]
            evidence = fixture["evidence"]
            assert isinstance(plan_path, Path)
            assert isinstance(evidence, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            original_metadata = os.stat(evidence, follow_symlinks=False)
            original_identity = MODULE._pc_recovery_identity(original_metadata)
            original_access = MODULE._pc_recovery_access(original_metadata)
            replacement = evidence.with_name("replacement-evidence")
            replacement.write_bytes(evidence.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, evidence)
            replacement_metadata = os.stat(evidence, follow_symlinks=False)
            self.assertNotEqual(
                MODULE._pc_recovery_identity(replacement_metadata),
                original_identity,
            )
            self.assertEqual(
                MODULE._pc_recovery_access(replacement_metadata),
                original_access,
            )
            self.assertEqual(evidence.read_bytes(), b"retained\n")
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "plan no longer matches retained evidence",
            ):
                MODULE.execute_private_control_recovery(
                    MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )
            self.assert_retained_recovery_unpublished()

    def test_retained_recovery_rejects_a_busy_legacy_lease(self) -> None:
        with self.retained_recovery_scope("second-busy-lease") as fixture:
            tool = fixture["tool"]
            quarantine = fixture["quarantine"]
            plan_path = fixture["plan_path"]
            assert isinstance(tool, Path)
            assert isinstance(quarantine, Path)
            assert isinstance(plan_path, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            quarantine_identity = MODULE._pc_recovery_identity(
                os.stat(quarantine, follow_symlinks=False)
            )
            real_flock = fcntl.flock

            def fail_second_lease(file_fd: int, operation: int) -> None:
                if (
                    operation == fcntl.LOCK_EX | fcntl.LOCK_NB
                    and MODULE._pc_recovery_identity(os.fstat(file_fd))
                    == quarantine_identity
                ):
                    raise BlockingIOError(
                        errno.EWOULDBLOCK,
                        "simulated quarantine lock conflict",
                    )
                real_flock(file_fd, operation)

            with mock.patch.object(
                MODULE.fcntl,
                "flock",
                side_effect=fail_second_lease,
            ):
                with self.assertRaisesRegex(MODULE.SyncError, "busy"):
                    MODULE.execute_private_control_recovery(
                        MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                        plan_path,
                    )
            self.assertEqual(MODULE._PC_RECOVERY_RETAINED_CLOSE_FENCE, ())
            tool_fd = os.open(
                tool,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                fcntl.flock(tool_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                fcntl.flock(tool_fd, fcntl.LOCK_UN)
                os.close(tool_fd)
            self.assert_retained_recovery_unpublished()

    def test_retained_recovery_receipt_only_state_is_retryable(self) -> None:
        with self.retained_recovery_scope("receipt-only") as fixture:
            plan_path = fixture["plan_path"]
            assert isinstance(plan_path, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            original_publish = MODULE._pc_recovery_publish_document

            def fail_before_marker(*args: object, **kwargs: object):
                if args[1] == MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME:
                    raise MODULE.SyncError("simulated marker crash")
                return original_publish(*args, **kwargs)

            with mock.patch.object(
                MODULE,
                "_pc_recovery_publish_document",
                side_effect=fail_before_marker,
            ):
                with self.assertRaisesRegex(MODULE.SyncError, "simulated marker crash"):
                    MODULE.execute_private_control_recovery(
                        MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                        plan_path,
                    )
            receipt_path = (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            )
            marker_path = (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            )
            self.assertTrue(receipt_path.is_file())
            self.assertFalse(marker_path.exists())
            receipt_metadata = os.stat(receipt_path, follow_symlinks=False)
            receipt_payload = receipt_path.read_bytes()
            blocked = MODULE._mirror_quarantine_audit()
            self.assertEqual(blocked.classification, "inconclusive")
            self.assertEqual(
                blocked.reason_code,
                MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
            )
            self.assertIn(
                "receipt exists without its cutover marker",
                blocked.detail,
            )
            result = MODULE.execute_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            self.assertEqual(result["status"], "executed")
            self.assertTrue(marker_path.is_file())
            final_receipt = os.stat(receipt_path, follow_symlinks=False)
            self.assertEqual(
                MODULE._pc_recovery_identity(final_receipt),
                MODULE._pc_recovery_identity(receipt_metadata),
            )
            self.assertEqual(
                MODULE._pc_recovery_access(final_receipt),
                MODULE._pc_recovery_access(receipt_metadata),
            )
            self.assertEqual(receipt_path.read_bytes(), receipt_payload)
            marker = MODULE._pc_recovery_load_json(
                marker_path.read_bytes(),
                "test cutover marker",
            )
            self.assertEqual(marker["primary_receipt"], result["execute"]["receipt"])

    def test_retained_recovery_partial_pending_receipt_is_retryable(self) -> None:
        with self.retained_recovery_scope("pending-receipt") as fixture:
            plan_path = fixture["plan_path"]
            assert isinstance(plan_path, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            real_write = os.write
            injected = False

            def fail_after_partial_write(file_fd: int, payload: bytes) -> int:
                nonlocal injected
                if not injected:
                    injected = True
                    real_write(file_fd, payload[: max(1, len(payload) // 2)])
                    raise OSError(errno.EIO, "simulated interrupted receipt write")
                return real_write(file_fd, payload)

            with mock.patch.object(
                MODULE.os,
                "write",
                side_effect=fail_after_partial_write,
            ):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "cannot write pending primary recovery receipt",
                ):
                    MODULE.execute_private_control_recovery(
                        MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                        plan_path,
                    )
            pending = tuple(
                self.mirror_private_control_parent.glob(
                    f".{MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME}"
                    ".pending-*"
                )
            )
            self.assertTrue(injected)
            self.assertEqual(len(pending), 1)
            pending_identity = MODULE._pc_recovery_identity(
                os.stat(pending[0], follow_symlinks=False)
            )
            self.assert_retained_recovery_unpublished()
            result = MODULE.execute_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            self.assertEqual(result["status"], "executed")
            receipt_path = (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            )
            self.assertEqual(
                MODULE._pc_recovery_identity(
                    os.stat(receipt_path, follow_symlinks=False)
                ),
                pending_identity,
            )
            self.assertEqual(
                tuple(
                    self.mirror_private_control_parent.glob(
                        f".{MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME}"
                        ".pending-*"
                    )
                ),
                (),
            )
            self.assertEqual(
                tuple(
                    self.mirror_private_control_parent.glob(
                        f".{MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME}"
                        ".pending-*"
                    )
                ),
                (),
            )

    def test_retained_recovery_retry_rejects_receipt_replacement(self) -> None:
        with self.retained_recovery_scope("receipt-replacement") as fixture:
            plan_path = fixture["plan_path"]
            assert isinstance(plan_path, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            result = MODULE.execute_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            self.assertEqual(result["status"], "executed")
            receipt_path = (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            )
            marker_path = (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            )
            receipt_payload = receipt_path.read_bytes()
            receipt_metadata = os.stat(receipt_path, follow_symlinks=False)
            marker_metadata = os.stat(marker_path, follow_symlinks=False)
            marker_payload = marker_path.read_bytes()
            replacement = receipt_path.with_name("replacement-receipt")
            replacement.write_bytes(receipt_payload)
            replacement.chmod(0o400)
            os.replace(replacement, receipt_path)
            replaced_metadata = os.stat(receipt_path, follow_symlinks=False)
            self.assertNotEqual(
                MODULE._pc_recovery_identity(replaced_metadata),
                MODULE._pc_recovery_identity(receipt_metadata),
            )
            self.assertEqual(
                MODULE._pc_recovery_access(replaced_metadata),
                MODULE._pc_recovery_access(receipt_metadata),
            )
            self.assertEqual(receipt_path.read_bytes(), receipt_payload)
            blocked = MODULE._mirror_quarantine_audit()
            self.assertEqual(blocked.classification, "inconclusive")
            self.assertEqual(
                blocked.reason_code,
                MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
            )
            self.assertIn(
                "cutover marker receipt binding changed",
                blocked.detail,
            )
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "marker receipt binding changed",
            ):
                MODULE.execute_private_control_recovery(
                    MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )
            final_marker = os.stat(marker_path, follow_symlinks=False)
            self.assertEqual(
                MODULE._pc_recovery_identity(final_marker),
                MODULE._pc_recovery_identity(marker_metadata),
            )
            self.assertEqual(marker_path.read_bytes(), marker_payload)

    def test_retained_recovery_marker_durability_retry_preserves_identity(
        self,
    ) -> None:
        with self.retained_recovery_scope("marker-retry") as fixture:
            plan_path = fixture["plan_path"]
            assert isinstance(plan_path, Path)
            MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            receipt_path = (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            )
            marker_path = (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            )
            real_fsync = os.fsync
            injected = False

            def fail_after_marker_publish(file_fd: int) -> None:
                nonlocal injected
                if marker_path.exists():
                    parent_identity = MODULE._pc_recovery_identity(
                        os.stat(
                            self.mirror_private_control_parent,
                            follow_symlinks=False,
                        )
                    )
                    if (
                        not injected
                        and MODULE._pc_recovery_identity(os.fstat(file_fd))
                        == parent_identity
                    ):
                        injected = True
                        real_fsync(file_fd)
                        raise OSError(
                            errno.EIO,
                            "simulated marker-parent fsync failure",
                        )
                real_fsync(file_fd)

            with mock.patch.object(
                MODULE.os,
                "fsync",
                side_effect=fail_after_marker_publish,
            ):
                with self.assertRaisesRegex(MODULE.SyncError, "cannot durably bind"):
                    MODULE.execute_private_control_recovery(
                        MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                        plan_path,
                    )
            self.assertTrue(injected)
            self.assertTrue(receipt_path.is_file())
            self.assertTrue(marker_path.is_file())
            marker_metadata = os.stat(marker_path, follow_symlinks=False)
            marker_payload = marker_path.read_bytes()

            primary_identity = MODULE._pc_recovery_identity(
                os.stat(
                    self.mirror_private_control_parent,
                    follow_symlinks=False,
                )
            )
            retry_fsyncs = 0

            def track_primary_fsync(file_fd: int) -> None:
                nonlocal retry_fsyncs
                if MODULE._pc_recovery_identity(os.fstat(file_fd)) == primary_identity:
                    retry_fsyncs += 1
                real_fsync(file_fd)

            with mock.patch.object(
                MODULE.os,
                "fsync",
                side_effect=track_primary_fsync,
            ):
                result = MODULE.execute_private_control_recovery(
                    MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )
            self.assertEqual(result["status"], "executed")
            self.assertGreaterEqual(retry_fsyncs, 1)
            final_marker = os.stat(marker_path, follow_symlinks=False)
            self.assertEqual(
                MODULE._pc_recovery_identity(final_marker),
                MODULE._pc_recovery_identity(marker_metadata),
            )
            self.assertEqual(
                MODULE._pc_recovery_access(final_marker),
                MODULE._pc_recovery_access(marker_metadata),
            )
            self.assertEqual(marker_path.read_bytes(), marker_payload)

    def test_retained_recovery_transitions_audit_and_strict_doctor(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-retained-parent"
        shared_parent.mkdir(mode=0o700)
        tool = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        evidence = quarantine / "retained-evidence"
        evidence.write_bytes(b"retained\n")
        evidence.chmod(0o600)
        primary_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_PRIMARY_ROOT_ID,
            parent_path=self.mirror_private_control_parent,
            allocate=True,
            account_home=self.root,
            shared_parent=False,
        )
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        before = snapshot_tree(shared_parent)
        plan_path = self.root / "legacy-retained-recovery-plan.json"

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            pending_audit = MODULE._mirror_quarantine_audit()
            plan = MODULE.plan_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            result = MODULE.execute_private_control_recovery(
                MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
            adopted_audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(pending_audit.classification, "inconclusive")
        self.assertEqual(
            pending_audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        self.assertEqual(plan["disposition"], "adopt-retained-in-place")
        self.assertEqual(result["status"], "executed")
        adopted_legacy_audit = next(
            root_audit
            for root_audit in adopted_audit.root_audits
            if root_audit.root_id == MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID
        )
        self.assertEqual(
            adopted_legacy_audit.classification,
            "adopted-retained-in-place",
        )
        self.assertNotIn(
            adopted_audit.classification,
            {"saturated", "inconclusive"},
        )
        self.assertIsNone(adopted_audit.reason_code)
        self.assertEqual(snapshot_tree(shared_parent), before)
        self.assertTrue(
            (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            ).is_file()
        )
        self.assertTrue(
            (
                self.mirror_private_control_parent
                / MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            ).is_file()
        )

        def report(audit: MODULE.MirrorQuarantineAudit) -> MODULE.SchedulerReport:
            return MODULE.SchedulerReport(
                platform="linux",
                installed=True,
                enabled=True,
                config_paths=(self.root / "scheduler.timer",),
                interval_minutes=17,
                runner=self.home / "bin" / "codex-personal-sync",
                stable_runner=True,
                mode="public",
                base_repo="owner/public-sync",
                private_repo=None,
                last_attempt=None,
                recent_success=None,
                current_releases=(),
                failure_reason=None,
                command="run-scheduled",
                repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
                quarantine_batches=0,
                mirror_quarantine=audit,
                daemon_query=MODULE.SchedulerDaemonQuery("enabled"),
            )

        for audit, expected_status in (
            (pending_audit, 1),
            (adopted_audit, 0),
        ):
            with (
                self.subTest(classification=audit.classification),
                mock.patch.object(
                    MODULE,
                    "scheduler_report",
                    return_value=report(audit),
                ),
                mock.patch.object(
                    MODULE,
                    "audit_active_skills",
                    return_value=[],
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = MODULE.main(
                    [
                        "doctor",
                        "--home",
                        str(self.home),
                        "--platform",
                        "linux",
                        "--strict",
                    ]
                )
            self.assertEqual(status, expected_status)

    def test_mirror_primary_alias_matrix_is_rejected_before_child_open(
        self,
    ) -> None:
        tool = self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        parent_identity = MODULE._mirror_object_identity(
            self.mirror_private_control_parent.stat()
        )
        real_metadata = MODULE._mirror_private_control_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory

        for scenario in ("parent-child", "child-child"):
            with self.subTest(scenario=scenario):
                tool_record = None
                child_opens: list[str] = []

                def aliased_metadata(parent_fd, name, label):
                    nonlocal tool_record
                    observed = real_metadata(parent_fd, name, label)
                    assert observed is not None
                    if name == MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME:
                        tool_record = observed
                        if scenario == "parent-child":
                            return parent_identity, observed[1]
                        return observed
                    if scenario == "child-child":
                        assert tool_record is not None
                        return tool_record
                    return observed

                def reject_child_open(parent_fd, parent_path, name, label):
                    if name in {
                        MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME,
                        MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
                    }:
                        child_opens.append(name)
                        self.fail(f"aliased mirror child was opened: {name}")
                    return real_bind_child(parent_fd, parent_path, name, label)

                with (
                    mock.patch.object(
                        MODULE,
                        "_mirror_private_control_child_metadata",
                        side_effect=aliased_metadata,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_bind_mirror_audit_child_directory",
                        side_effect=reject_child_open,
                    ),
                ):
                    audit = MODULE._mirror_quarantine_audit()

                self.assertEqual(audit.classification, "inconclusive")
                self.assertEqual(
                    audit.reason_code,
                    MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
                )
                self.assertRegex(
                    audit.detail,
                    "fixed child roles alias|fixed child aliases its own parent",
                )
                self.assertEqual(child_opens, [])

    def test_mirror_legacy_quarantine_without_tool_is_recovery_pending(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-quarantine-without-tool"
        shared_parent.mkdir(mode=0o700)
        quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        quarantine.mkdir(mode=0o700)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        before = snapshot_tree(shared_parent)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        legacy_audit = audit.root_audits[1]
        self.assertEqual(legacy_audit.classification, "inconclusive")
        self.assertEqual(
            legacy_audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        self.assertIn("without its coordination tool root", legacy_audit.detail)
        self.assertEqual(snapshot_tree(shared_parent), before)

    def test_mirror_quarantine_audit_holds_directory_leases_without_mutation(
        self,
    ) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)
        real_inventory = MODULE._bounded_mirror_directory_names
        observed_lock_order: list[str] = []

        def assert_shared_lease(directory_fd, *, limit, label):
            path = tool_root if label == "mirror private tool root" else quarantine
            contender_fd = os.open(path, MODULE._source_directory_flags())
            try:
                with self.assertRaises(BlockingIOError):
                    MODULE.fcntl.flock(
                        contender_fd,
                        MODULE.fcntl.LOCK_EX | MODULE.fcntl.LOCK_NB,
                    )
            finally:
                os.close(contender_fd)
            observed_lock_order.append(label)
            return real_inventory(
                directory_fd,
                limit=limit,
                label=label,
            )

        with mock.patch.object(
            MODULE,
            "_bounded_mirror_directory_names",
            side_effect=assert_shared_lease,
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "available")
        self.assertEqual(audit.entry_count, 0)
        self.assertEqual(
            observed_lock_order,
            [
                "mirror private tool root",
                "mirror durable quarantine segment",
            ],
        )
        self.assertEqual(len(audit.owner_records), 1)
        self.assertEqual(audit.owner_records[0].state, "stale")
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_retains_cross_root_owner_record(self) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.cccccccccccccccccccccccccccccccc"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": "wrong-primary-root-v9",
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "dddddddddddddddddddddddddddddddd",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)

        audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(len(audit.owner_records), 1)
        self.assertEqual(
            audit.owner_records[0].reason_code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

        report = MODULE.scheduler_report(self.home, "linux")
        report_audit = report.mirror_quarantine
        assert report_audit is not None
        failure_detail = MODULE._mirror_quarantine_failure_detail(report_audit)
        self.assertIn(
            (
                MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
                failure_detail,
            ),
            report.failures,
        )
        with (
            mock.patch.object(MODULE, "scheduler_report", return_value=report),
            mock.patch.object(MODULE, "audit_active_skills", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _doctor_report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        matching = [issue for issue in issues if issue.detail == failure_detail]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0].code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(matching[0].path, report_audit.path)
        self.assertNotEqual(matching[0].path, self.home)
        self.assertNotIn(
            "scheduler-failure",
            {issue.code for issue in matching},
        )

    def test_mirror_walkers_transfer_fd_before_effectful_close_error(self) -> None:
        real_close = MODULE.os.close
        account_home_candidate = Path("/usr")
        account_home_metadata = account_home_candidate.lstat()
        account_home_mode, account_home_uid, _account_home_gid = (
            MODULE._mirror_access_policy(account_home_metadata)
        )
        self.assertTrue(stat.S_ISDIR(account_home_metadata.st_mode))
        self.assertFalse(stat.S_ISLNK(account_home_metadata.st_mode))
        self.assertEqual(account_home_uid, 0)
        self.assertFalse(account_home_mode & 0o022)

        def invoke_ancestry() -> None:
            candidate_fd = os.open(
                self.mirror_private_control_parent,
                MODULE._source_directory_flags(),
            )
            try:
                MODULE._mirror_directory_is_at_or_below(
                    candidate_fd,
                    (-1, -1, stat.S_IFDIR),
                    label="fault-injected candidate",
                )
            finally:
                os.close(candidate_fd)

        for label, invoke in (
            (
                "account-home",
                lambda: self.production_account_home_binder(
                    account_home_candidate
                ),
            ),
            ("ancestry", invoke_ancestry),
        ):
            with self.subTest(label=label):
                close_calls: list[int] = []
                failed_fd: int | None = None

                def fail_first_close_after_effect(descriptor: int) -> None:
                    nonlocal failed_fd
                    close_calls.append(descriptor)
                    real_close(descriptor)
                    if failed_fd is None:
                        failed_fd = descriptor
                        raise OSError(f"injected {label} close-after-effect")

                with (
                    mock.patch.object(
                        MODULE.os,
                        "close",
                        side_effect=fail_first_close_after_effect,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        f"injected {label} close-after-effect",
                    ),
                ):
                    invoke()

                assert failed_fd is not None
                self.assertEqual(close_calls.count(failed_fd), 1)
                self.assertGreaterEqual(len(set(close_calls)), 2)

    def test_owner_audit_aggregates_unlock_and_effectful_close_errors(self) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        )
        owner_name = f"{private_name}.owner.json"
        owner_path = tool_root / owner_name
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "ffffffffffffffffffffffffffffffff",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        owner_identity = MODULE._mirror_object_identity(owner_path.stat())
        tool_fd = os.open(tool_root, MODULE._source_directory_flags())
        real_flock = MODULE.fcntl.flock
        real_close = MODULE.os.close
        owner_close_calls: list[int] = []

        def fail_owner_unlock(descriptor: int, operation: int) -> None:
            if (
                operation == MODULE.fcntl.LOCK_UN
                and MODULE._mirror_object_identity(os.fstat(descriptor))
                == owner_identity
            ):
                raise OSError("injected owner unlock failure")
            real_flock(descriptor, operation)

        def fail_owner_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if descriptor_identity == owner_identity:
                owner_close_calls.append(descriptor)
                raise OSError("injected owner close-after-effect")

        try:
            with (
                mock.patch.object(
                    MODULE.fcntl,
                    "flock",
                    side_effect=fail_owner_unlock,
                ),
                mock.patch.object(
                    MODULE.os,
                    "close",
                    side_effect=fail_owner_close_after_effect,
                ),
                self.assertRaises(MODULE.SyncError) as caught,
            ):
                MODULE._audit_mirror_owner_record(
                    tool_fd,
                    owner_name,
                    MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                )
        finally:
            os.close(tool_fd)

        self.assertIn("injected owner unlock failure", str(caught.exception))
        self.assertIn("injected owner close-after-effect", str(caught.exception))
        self.assertEqual(len(owner_close_calls), 1)

    def test_initial_root_cleanup_failure_does_not_skip_later_roots(self) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        quarantine_identity = MODULE._mirror_object_identity(quarantine.stat())
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_specs = []
        for index in range(2):
            parent = self.root / f"cleanup-later-root-{index}"
            parent.mkdir(mode=0o700)
            legacy_specs.append(
                MODULE.MirrorPrivateControlRootSpec(
                    root_id=f"cleanup-later-root-{index}",
                    parent_path=parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
            )
        real_acquire = MODULE._acquire_mirror_audit_shared_lock
        real_close = MODULE.os.close
        cleanup_enabled = False
        failed_fds: list[int] = []

        def enable_fault_after_quarantine_lease(descriptor: int, label: str) -> None:
            nonlocal cleanup_enabled
            real_acquire(descriptor, label)
            if label == "mirror durable quarantine segment":
                cleanup_enabled = True

        def fail_quarantine_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if (
                cleanup_enabled
                and descriptor_identity == quarantine_identity
                and not failed_fds
            ):
                failed_fds.append(descriptor)
                raise OSError("injected initial-root close-after-effect")

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, *legacy_specs),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_acquire_mirror_audit_shared_lock",
                side_effect=enable_fault_after_quarantine_lease,
            ),
            mock.patch.object(
                MODULE.os,
                "close",
                side_effect=fail_quarantine_close_after_effect,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(len(failed_fds), 1)
        self.assertEqual(
            [root.root_id for root in audit.root_audits],
            [primary_spec.root_id, *(spec.root_id for spec in legacy_specs)],
        )
        self.assertEqual(audit.root_audits[0].classification, "inconclusive")
        self.assertIn("injected initial-root close-after-effect", audit.detail)
        for spec in legacy_specs:
            self.assertIn(spec.root_id, audit.detail)

    def test_absence_and_terminal_close_failures_retain_body_and_coverage(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        absence_anchor = self.root / "absence-close-anchor"
        absence_anchor.mkdir(mode=0o700)
        absent_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="absence-close-root",
            parent_path=absence_anchor / "missing-parent",
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        later_parent = self.root / "absence-close-later"
        later_parent.mkdir(mode=0o700)
        later_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="absence-close-later",
            parent_path=later_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        anchor_identity = MODULE._mirror_object_identity(absence_anchor.stat())
        real_close = MODULE.os.close
        absence_faults: list[int] = []

        def fail_absence_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if descriptor_identity == anchor_identity and not absence_faults:
                absence_faults.append(descriptor)
                raise OSError("injected absence close-after-effect")

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, absent_spec, later_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE.os,
                "close",
                side_effect=fail_absence_close_after_effect,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(len(absence_faults), 1)
        self.assertEqual(
            [root.root_id for root in audit.root_audits],
            [primary_spec.root_id, absent_spec.root_id, later_spec.root_id],
        )
        self.assertIn("injected absence close-after-effect", audit.detail)
        self.assertIn(later_spec.root_id, audit.detail)

        first_parent = self.root / "terminal-close-first"
        second_anchor = self.root / "terminal-close-second-anchor"
        first_parent.mkdir(mode=0o700)
        second_anchor.mkdir(mode=0o700)
        first_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-close-first",
            parent_path=first_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        second_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-close-second",
            parent_path=second_anchor / "missing-parent",
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        first_identity = MODULE._mirror_object_identity(first_parent.stat())
        first_policy = MODULE._mirror_access_policy(first_parent.stat())
        second_receipt = MODULE._capture_mirror_quarantine_parent_absence(second_spec)
        first_audit = MODULE.MirrorQuarantineAudit(
            classification="available",
            path=first_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
            entry_count=0,
            entry_limit=MODULE.MIRROR_DURABLE_QUARANTINE_ENTRY_LIMIT,
            count_is_lower_bound=False,
            segment_identity=None,
            segment_access_policy=None,
            root_id=first_spec.root_id,
            root_receipt=MODULE.MirrorQuarantineRootReceipt(
                root_id=first_spec.root_id,
                parent_path=first_parent,
                scope="parent-only",
                parent_identity=(
                    first_identity[0],
                    first_identity[1] + 1,
                    first_identity[2],
                ),
                parent_access_policy=first_policy,
            ),
        )
        second_audit = MODULE.MirrorQuarantineAudit(
            classification="absent",
            path=second_spec.parent_path / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
            entry_count=0,
            entry_limit=MODULE.MIRROR_DURABLE_QUARANTINE_ENTRY_LIMIT,
            count_is_lower_bound=False,
            segment_identity=None,
            segment_access_policy=None,
            root_id=second_spec.root_id,
            root_receipt=second_receipt,
        )
        terminal_faults: list[int] = []

        def fail_terminal_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if descriptor_identity == first_identity and not terminal_faults:
                terminal_faults.append(descriptor)
                raise OSError("injected terminal close-after-effect")

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (first_spec, second_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE.os,
                "close",
                side_effect=fail_terminal_close_after_effect,
            ),
        ):
            updated, detail = MODULE._revalidate_mirror_quarantine_registry(
                [first_audit, second_audit]
            )

        self.assertEqual(len(terminal_faults), 1)
        assert detail is not None
        self.assertIn("identity or access policy changed", updated[0].detail)
        self.assertIn("injected terminal close-after-effect", updated[0].detail)
        self.assertIn(first_spec.root_id, detail)
        self.assertIn(second_spec.root_id, detail)
        self.assertEqual(updated[1].classification, "absent")

    def test_legacy_owner_root_mismatch_routes_exactly_once(self) -> None:
        shared_parent = self.root / "legacy-owner-mismatch"
        shared_parent.mkdir(mode=0o700)
        tool_root = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        tool_root.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.11111111111111111111111111111111"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": "wrong-legacy-root",
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "22222222222222222222222222222222",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="legacy-owner-mismatch",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(audit.root_id, legacy_spec.root_id)
        self.assertEqual(
            audit.path,
            shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
        )
        report_audit = report.mirror_quarantine
        assert report_audit is not None
        failure_detail = MODULE._mirror_quarantine_failure_detail(report_audit)
        self.assertIn(
            (
                MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
                failure_detail,
            ),
            report.failures,
        )
        with (
            mock.patch.object(MODULE, "scheduler_report", return_value=report),
            mock.patch.object(MODULE, "audit_active_skills", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _doctor_report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        matching = [issue for issue in issues if issue.detail == failure_detail]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0].code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(
            matching[0].path,
            shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
        )
        self.assertNotIn(
            "scheduler-failure",
            {issue.code for issue in matching},
        )

    def test_mirror_quarantine_audit_reports_busy_tool_root_without_scanning(
        self,
    ) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        before = snapshot_tree(self.mirror_private_control_parent)
        tool_fd = os.open(tool_root, MODULE._source_directory_flags())
        try:
            MODULE.fcntl.flock(tool_fd, MODULE.fcntl.LOCK_EX)
            audit = MODULE._mirror_quarantine_audit()
        finally:
            MODULE.fcntl.flock(tool_fd, MODULE.fcntl.LOCK_UN)
            os.close(tool_fd)

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIsNone(audit.entry_count)
        self.assertIn("busy with an active writer", audit.detail)
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_does_not_lock_without_coordination_root(
        self,
    ) -> None:
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        before = snapshot_tree(self.mirror_private_control_parent)

        with mock.patch.object(
            MODULE,
            "_acquire_mirror_audit_shared_lock",
        ) as acquire:
            audit = MODULE._mirror_quarantine_audit()

        acquire.assert_not_called()
        self.assertEqual(audit.classification, "inconclusive")
        self.assertIsNone(audit.entry_count)
        self.assertIn(
            "exists without its coordination tool root",
            audit.detail,
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_classifies_deep_owner_json_as_invalid(
        self,
    ) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.cccccccccccccccccccccccccccccccc"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_bytes(b"[" * 1_200 + b"0" + b"]" * 1_200)
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)

        audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "available")
        self.assertEqual(len(audit.owner_records), 1)
        self.assertEqual(audit.owner_records[0].state, "invalid")
        self.assertIn("schema", audit.owner_records[0].detail)
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_detects_final_access_policy_drift(
        self,
    ) -> None:
        (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        ).mkdir(mode=0o700)
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        before = snapshot_tree(self.mirror_private_control_parent)
        real_revalidate = MODULE._revalidate_mirror_audit_directory
        quarantine_revalidations = 0

        def mutate_after_first_quarantine_revalidation(
            path,
            directory_fd,
            identity,
            access_policy,
            label,
        ):
            nonlocal quarantine_revalidations
            real_revalidate(
                path,
                directory_fd,
                identity,
                access_policy,
                label,
            )
            if (
                label == "mirror durable quarantine segment"
                and quarantine_revalidations == 0
            ):
                quarantine.chmod(0o755)
                quarantine_revalidations += 1

        try:
            with mock.patch.object(
                MODULE,
                "_revalidate_mirror_audit_directory",
                side_effect=mutate_after_first_quarantine_revalidation,
            ):
                audit = MODULE._mirror_quarantine_audit()
        finally:
            quarantine.chmod(0o700)

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn("access policy changed during audit", audit.detail)
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_handoff_lists_only_durable_recovery_records(
        self,
    ) -> None:
        def owner(
            name: str,
            *,
            state: str,
            private_state: str | None,
        ) -> MODULE.MirrorQuarantineOwnerRecord:
            return MODULE.MirrorQuarantineOwnerRecord(
                name=name,
                identity=(1, 2, stat.S_IFREG),
                access_policy=(0o600, os.geteuid(), os.getegid()),
                sha256="a" * 64,
                state=state,
                owner_pid=123,
                owner_nonce="b" * 32,
                phase="cleanup",
                private_name=f"{name}.private",
                expected_private_identity=(3, 4, stat.S_IFDIR),
                observed_private_identity=(
                    (3, 4, stat.S_IFDIR)
                    if private_state in {"matching", "mismatched"}
                    else None
                ),
                private_state=private_state,
            )

        audit = MODULE.MirrorQuarantineAudit(
            classification="saturated",
            path=(
                self.mirror_private_control_parent
                / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
            ),
            entry_count=10,
            entry_limit=10,
            count_is_lower_bound=False,
            segment_identity=(5, 6, stat.S_IFDIR),
            segment_access_policy=(0o700, os.geteuid(), os.getegid()),
            owner_records=(
                owner("missing.owner.json", state="stale", private_state="missing"),
                owner(
                    "matching.owner.json",
                    state="stale",
                    private_state="matching",
                ),
                owner(
                    "mismatched.owner.json",
                    state="stale",
                    private_state="mismatched",
                ),
                owner("invalid.owner.json", state="invalid", private_state=None),
                owner("active.owner.json", state="active", private_state=None),
            ),
        )

        detail = MODULE._mirror_quarantine_failure_detail(audit)

        self.assertIn("missing.owner.json", detail)
        self.assertIn("matching.owner.json", detail)
        self.assertIn("sha256=" + "a" * 64, detail)
        self.assertIn("nonce=" + "b" * 32, detail)
        self.assertNotIn("mismatched.owner.json", detail)
        self.assertNotIn("invalid.owner.json", detail)
        self.assertNotIn("active.owner.json", detail)

    def test_native_scheduler_commands_use_closed_environment(self) -> None:
        injected = {
            "LD_PRELOAD": "/tmp/injected.so",
            "LD_LIBRARY_PATH": "/tmp/injected",
            "DYLD_INSERT_LIBRARIES": "/tmp/injected.dylib",
            "BASH_ENV": "/tmp/bash-env",
            "ENV": "/tmp/shell-env",
            "PYTHONPATH": "/tmp/python",
        }
        captured: dict[str, object] = {}
        real_popen = subprocess.Popen

        def capture_popen(args, **kwargs):
            captured.update(kwargs)
            return real_popen(args, **kwargs)

        with (
            mock.patch.dict(MODULE.os.environ, injected, clear=False),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                return_value=[sys.executable, "-c", "pass"],
            ),
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_popen,
            ),
        ):
            MODULE._run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=False,
            )

        environment = captured["env"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["LC_ALL"], "C")
        for name in injected:
            self.assertNotIn(name, environment)

    def test_active_skill_audit_detects_root_replacement_from_bound_fd(
        self,
    ) -> None:
        skills = self.home / "skills"
        original = skills / "original"
        original.mkdir(parents=True)
        (original / "SKILL.md").write_text(
            "---\nname: original\n---\n",
            encoding="utf-8",
        )
        replacement = self.home / "replacement-skills"
        injected = replacement / "injected"
        injected.mkdir(parents=True)
        (injected / "SKILL.md").write_text(
            "---\nname: injected\n---\n",
            encoding="utf-8",
        )
        retained = self.home / "retained-skills"
        real_names = MODULE._bounded_skill_child_names
        swapped = False

        def list_then_swap(directory_fd: int) -> tuple[str, ...]:
            nonlocal swapped
            names = real_names(directory_fd)
            if not swapped:
                swapped = True
                skills.rename(retained)
                replacement.rename(skills)
            return names

        with mock.patch.object(
            MODULE,
            "_bounded_skill_child_names",
            side_effect=list_then_swap,
        ):
            issues = MODULE.audit_active_skills(self.home)

        self.assertTrue(swapped)
        self.assertIn("skills-root-unsafe", {issue.code for issue in issues})
        self.assertFalse(any("injected" in issue.detail for issue in issues))

    def test_uninstall_refuses_scheduler_symlink_without_deleting_target(
        self,
    ) -> None:
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        target = self.root / "outside-service"
        target.write_text("keep\n", encoding="utf-8")
        paths.systemd_service.symlink_to(target)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "non-file sync state",
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=False,
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(paths.systemd_service.is_symlink())

    def test_active_skill_scan_is_bounded_and_requires_closed_frontmatter(
        self,
    ) -> None:
        skills = self.home / "skills"
        skills.mkdir()
        for name in ("one", "two", "three"):
            root = skills / name
            root.mkdir()
            (root / "SKILL.md").write_text(
                f"---\nname: {name}\n---\n",
                encoding="utf-8",
            )
        with mock.patch.object(MODULE, "MAX_ACTIVE_SKILL_ENTRIES", 2):
            issues = MODULE.audit_active_skills(self.home)
        self.assertIn("skills-root-overflow", {issue.code for issue in issues})

        shutil.rmtree(skills)
        malformed = skills / "malformed"
        malformed.mkdir(parents=True)
        (malformed / "SKILL.md").write_text(
            "---\nname: missing-close\n",
            encoding="utf-8",
        )
        issues = MODULE.audit_active_skills(self.home)
        self.assertIn("invalid-frontmatter", {issue.code for issue in issues})

        shutil.rmtree(skills)
        large = skills / "large-body"
        large.mkdir(parents=True)
        (large / "SKILL.md").write_bytes(
            b"---\nname: large-body\n---\n"
            + b"x" * (MODULE.MAX_SKILL_FRONTMATTER_BYTES + 1024)
            + b"\xff"
        )
        issues = MODULE.audit_active_skills(self.home)
        self.assertIn("unmanaged-skill", {issue.code for issue in issues})
        self.assertNotIn("invalid-frontmatter", {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()
