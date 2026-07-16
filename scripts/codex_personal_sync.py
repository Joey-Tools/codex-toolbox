#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Callable
import contextlib
import ctypes
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
from graphlib import CycleError, TopologicalSorter
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, NoReturn
import unicodedata
import zlib


TAG_PREFIX = "personal-codex-"
TAG_RE = re.compile(r"^personal-codex-\d{8}-\d{6}-([0-9a-f]{7,40})$")
ASSET_RE = re.compile(r"^personal-codex-([0-9a-f]{40})\.tar\.gz$")
SHA256_RE = re.compile(r"^personal-codex-([0-9a-f]{40})\.sha256$")
RELEASE_DIR_RE = re.compile(r"^[0-9a-f]{40}$")
MANIFEST_RELATIVE_PATH = Path("personal_codex/sync-manifest.json")
MAX_RELEASE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 4300
MAX_ARCHIVE_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_CHECKSUM_BYTES = 64 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBER_PATH_BYTES = 4096
MAX_ARCHIVE_MEMBER_COMPONENT_BYTES = 255
MAX_ARCHIVE_MEMBER_PATH_DEPTH = 64
MAX_MANIFEST_TARGET_PATH_BYTES = 4096
MAX_MANIFEST_TARGET_COMPONENT_BYTES = 255
MAX_MANIFEST_TARGET_PATH_DEPTH = 64
STATE_RELATIVE_PATH = Path("state/managed-links.json")
MAX_MANAGED_STATE_BYTES = 16 * 1024 * 1024
QUARANTINE_RELATIVE_PATH = Path("quarantine")
PENDING_LINK_POINTER_NAME = ".personal-sync-pending-transaction.json"
PENDING_LINK_METADATA_NAME = "pending-transaction.json"
PENDING_STATE_BEFORE_EVIDENCE = PurePosixPath("pending", "state", "before")
PENDING_STATE_AFTER_EVIDENCE = PurePosixPath("pending", "state", "after")
PENDING_STATE_COMMIT_EVIDENCE = PurePosixPath(
    "pending", "state", "commit-evidence"
)
PENDING_STATE_COMMIT_MARKER = PurePosixPath("pending", "state", "committed")
PENDING_CLEANUP_INDEX_RELATIVE_PATH = Path("pending-cleanup")
PENDING_CLEANUP_TICKET_SUFFIX = ".json"
PENDING_CLEANUP_CURSOR_NAME = ".scan-cursor"
PENDING_CLEANUP_CURSOR_TEMP_NAME = ".scan-cursor.tmp"
PENDING_CLEANUP_RETAINED_PREFIX = ".retained-cleanup-"
PENDING_CLEANUP_ISOLATED_BATCH_PREFIX = ".cleanup-ready-"
PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX = ".cleanup-active-entry-"
PENDING_CLEANUP_RETAINED_ENTRY_PREFIX = ".cleanup-retained-entry-"
PENDING_CLEANUP_ENTRY_TOKEN_RE = re.compile(
    r"^([0-9a-f]{1,16})-([0-9a-f]{1,16})-"
    r"([0-9a-f]{1,16})-([0-9a-f]{1,16})-"
    r"([0-9a-f]{1,8})-([0-9a-f]{16})$"
)
MAX_PENDING_CLEANUP_TICKET_BYTES = 4096
PENDING_LINK_BATCH_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9]+$"
)
MAX_PENDING_LINK_BATCH_NAME_BYTES = 128
MAX_PENDING_LINK_RECORDS = 10_000
MAX_PENDING_LINK_CLAIMS = 20_000
MAX_PENDING_RELEASES = 10_000
MAX_PENDING_CLEANUP_BATCH_SCAN = 10_000
MAX_PENDING_CLEANUP_BATCHES_PER_RUN = 8
MAX_RETAINED_QUARANTINE_BATCHES = 8
MAX_PENDING_CLEANUP_DEPTH = MAX_MANIFEST_TARGET_PATH_DEPTH + 8
MAX_PENDING_CLEANUP_ENTRIES = (
    MAX_PENDING_LINK_RECORDS * (MAX_MANIFEST_TARGET_PATH_DEPTH + 8)
    + MAX_PENDING_LINK_CLAIMS * 2
    + 128
)
_MAX_PENDING_IDENTITY = (2**64 - 1, 2**64 - 1)
_MAX_PENDING_DIGEST = "f" * 64
_MAX_PENDING_LINK_TARGET = "\udcff" * MAX_ARCHIVE_MEMBER_PATH_BYTES
_MAX_PENDING_BATCH_NAME = (
    "00000000T000000Z-0-"
    + "0" * (MAX_PENDING_LINK_BATCH_NAME_BYTES - len("00000000T000000Z-0-"))
)
# A first-install transaction also records and claims the owner's current link.
MAX_MANIFEST_ACTIVE_LINKS = (
    min(MAX_PENDING_LINK_RECORDS, MAX_PENDING_LINK_CLAIMS) - 1
)
DEFAULT_RELEASE_REPO_ENV = "CODEX_PERSONAL_SYNC_DEFAULT_REPO"
DEFAULT_BASE_RELEASE_REPO_ENV = "CODEX_PERSONAL_SYNC_BASE_REPO"
DEFAULT_PUBLIC_RELEASE_REPO = "Joey-Tools/codex-toolbox"
PUBLIC_OWNER = "public"
OPTIONAL_PUBLIC_TARGETS = frozenset({PurePosixPath("AGENTS.md")})
SYNC_INTERNAL_TARGET = PurePosixPath("personal-sync")
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$"
)
REMOVED_LINK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
REMOVED_LINK_FIELDS = frozenset(
    {
        "id",
        "source",
        "target",
        "kind",
        "replacement_target",
        "retires_replacements",
        "legacy",
    }
)
LAUNCHD_LABEL = "io.github.joey-tools.codex-personal-sync"
LEGACY_LAUNCHD_LABELS = ("com.joeyteng.codex-personal-sync",)
SYSTEMD_UNIT = "codex-personal-sync"
DEFAULT_SCHEDULER_INTERVAL_MINUTES = 60
MACOS_SCHEDULER_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LINUX_SCHEDULER_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class SyncError(RuntimeError):
    pass


def _bounded_json_integer(raw_value: str) -> int:
    digits = raw_value[1:] if raw_value.startswith("-") else raw_value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits"
        )
    return int(raw_value)


class _ArchiveEntryExistsError(SyncError):
    pass


class _SafeReconcileApplyError(SyncError):
    """Reconciliation failed before an existing managed link was changed."""


@dataclass(frozen=True)
class ReleaseAssets:
    tag_name: str
    sha: str
    archive_name: str
    archive_id: int
    archive_size: int
    checksum_name: str
    checksum_id: int
    checksum_size: int


@dataclass(frozen=True)
class DownloadedRelease:
    repo: str
    assets: ReleaseAssets
    release_root: Path
    release_expectation: ReleaseTreeExpectation | None = None


@dataclass(frozen=True)
class BaseReleaseSpec:
    repo: str
    sha: str | None = None


@dataclass(frozen=True)
class LinkEntry:
    source: PurePosixPath
    target: PurePosixPath
    kind: str
    owner: str = PUBLIC_OWNER
    override: bool = False


@dataclass(frozen=True)
class RemovedLink:
    id: str
    source: PurePosixPath
    target: PurePosixPath
    kind: str
    owner: str
    replacement_target: PurePosixPath | None = None
    retires_replacements: tuple[str, ...] = ()
    legacy: bool = False


@dataclass(frozen=True)
class ManifestData:
    owner: str
    entries: list[LinkEntry]
    removed_links: list[RemovedLink]
    payload_digest: str | None = None
    base_release_repo: str | None = None
    base_release_sha: str | None = None


ReleaseTreeIdentity = tuple[dict[str, Any], ManifestData, str]
ReleaseTreeExpectation = tuple[ReleaseTreeIdentity, tuple[int, int]]


@dataclass
class InstallReleaseBinding:
    owner: str
    sha: str
    expected_identity: ReleaseTreeIdentity
    expected_directory_identity: tuple[int, int]
    releases_root: Path
    releases_fd: int
    release_fd: int


@dataclass(frozen=True)
class ActiveReleaseExpectation:
    owner: str
    sha: str
    manifest: ManifestData
    expectation: ReleaseTreeExpectation


@dataclass(frozen=True)
class LinkAction:
    action: str
    target: Path
    link_target: str
    kind: str


@dataclass(frozen=True)
class ManagedLinkRecord:
    source: PurePosixPath
    target: PurePosixPath
    kind: str
    owner: str
    link_target: str
    release_sha: str


@dataclass
class ManagedState:
    owners: dict[str, str]
    links: dict[PurePosixPath, ManagedLinkRecord]


@dataclass(frozen=True)
class PendingLinkRecord:
    index: int
    scope: str
    action: str
    target: PurePosixPath
    kind: str
    planned_snapshot: ReconcileTargetSnapshot
    source: PurePosixPath | None
    owner: str | None
    link_target: str | None
    release_sha: str | None
    before_evidence: PurePosixPath | None
    before_evidence_identity: tuple[int, int] | None
    backup: PurePosixPath | None
    stage: PurePosixPath | None
    stage_identity: tuple[int, int] | None
    evidence: PurePosixPath | None
    evidence_identity: tuple[int, int] | None

    def managed_record(self) -> ManagedLinkRecord:
        if (
            self.scope != "managed"
            or self.action not in {"create", "replace", "quarantine-replace"}
            or self.source is None
            or self.owner is None
            or self.link_target is None
            or self.release_sha is None
        ):
            raise SyncError(
                f"pending record does not describe a managed state claim: {self.target}"
            )
        return ManagedLinkRecord(
            source=self.source,
            target=self.target,
            kind=self.kind,
            owner=self.owner,
            link_target=self.link_target,
            release_sha=self.release_sha,
        )


@dataclass(frozen=True)
class PendingLinkClaim:
    index: int
    scope: str
    target: PurePosixPath
    kind: str
    source: PurePosixPath | None
    owner: str
    link_target: str
    release_sha: str
    parent_identity: tuple[int, int]
    link_identity: tuple[int, int]
    evidence: PurePosixPath


@dataclass(frozen=True)
class PendingReleaseExpectation:
    owner: str
    sha: str
    directory_identity: tuple[int, int]
    tree_sha256: str


@dataclass
class PendingLinkBatch:
    batch_root: Path
    batch_root_identity: tuple[int, int]
    records: tuple[PendingLinkRecord, ...]
    claims_before: tuple[PendingLinkClaim, ...]
    claims_after: tuple[PendingLinkClaim, ...]
    state_before: ManagedStateFileSnapshot
    state_after: ManagedStateFileSnapshot
    state_before_evidence: PurePosixPath | None
    state_after_evidence: PurePosixPath
    state_before_value: ManagedState
    state_after_value: ManagedState
    releases_before: tuple[PendingReleaseExpectation, ...]
    releases_after: tuple[PendingReleaseExpectation, ...]
    commit_evidence: ManagedStateFileSnapshot
    commit_evidence_path: PurePosixPath
    commit_marker_path: PurePosixPath
    pointer_snapshot: ManagedStateFileSnapshot | None = None


@dataclass(frozen=True)
class PendingBatchCleanupTicket:
    path: Path
    snapshot: ManagedStateFileSnapshot
    batch_root: Path
    batch_root_identity: tuple[int, int]
    marker_parent_identity: tuple[int, int]
    marker_file_identity: tuple[int, int]
    marker_mode: int
    marker_sha256: str


@dataclass(frozen=True)
class ReconcileAction:
    action: str
    target: Path
    link_target: str
    kind: str
    expected_link_target: str | None = None
    removed_link_key: str | None = None
    planned_snapshot: ReconcileTargetSnapshot | None = None


@dataclass(frozen=True)
class PendingLinkCapacityPlan:
    ordered_groups: tuple[tuple[str, tuple[ReconcileAction, ...]], ...]
    flattened_actions: tuple[ReconcileAction, ...]
    retired_absence_specs: tuple[tuple[PurePosixPath, ManagedLinkRecord], ...]
    retired_current_absence_specs: tuple[tuple[PurePosixPath, str], ...] = ()


@dataclass(frozen=True)
class ManifestTransitionCapacityProfile:
    owner: str
    state: ManagedState
    managed_state_size: int
    before_current_claim_size: int
    before_claim_sizes: dict[PurePosixPath, int]
    after_claim_size_sum: int
    after_claim_count: int
    release_size_sum: int
    release_count: int
    current_record_size: int
    create_record_sizes: dict[PurePosixPath, int]
    remove_record_sizes: dict[PurePosixPath, int]
    retired_absence_record_sizes: dict[PurePosixPath, int]
    historical_record_sizes: dict[PurePosixPath, int]


@dataclass(frozen=True)
class PendingLinkBatchPlan:
    capacity: PendingLinkCapacityPlan
    state_before_value: ManagedState


@dataclass(frozen=True)
class SymlinkSnapshot:
    parent_identity: tuple[int, int]
    link_identity: tuple[int, int]
    link_target: str


@dataclass(frozen=True)
class ReconcileTargetSnapshot:
    parent_identity: tuple[int, int] | None
    link_identity: tuple[int, int] | None = None
    link_target: str | None = None
    ancestor_identity: tuple[int, int] | None = None
    missing_parent_parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.parent_identity is None and self.link_identity is not None:
            raise ValueError("a missing reconcile parent cannot contain a link")
        if self.link_identity is None and self.link_target is not None:
            raise ValueError("an absent reconcile leaf cannot contain a link target")
        if self.parent_identity is None:
            if self.ancestor_identity is None or not self.missing_parent_parts:
                raise ValueError(
                    "a missing reconcile parent requires a bound existing ancestor"
                )
        elif self.missing_parent_parts:
            raise ValueError("an existing reconcile parent cannot have missing parts")


@dataclass
class ReconcileMutation:
    action: ReconcileAction
    backup: Path | None = None
    created_snapshot: SymlinkSnapshot | None = None


@dataclass
class ReconcileTransaction:
    batch_root: Path | None
    mutations: list[ReconcileMutation]


@dataclass(frozen=True)
class ManagedStateFileSnapshot:
    exists: bool
    payload: bytes | None = None
    mode: int | None = None
    parent_identity: tuple[int, int] | None = None
    file_identity: tuple[int, int] | None = None


@dataclass
class ManagedStateFileTransaction:
    before: ManagedStateFileSnapshot
    after: ManagedStateFileSnapshot
    batch_root: Path | None = None
    backup: Path | None = None
    published: bool = False
    state_parent_identity: tuple[int, int] | None = None
    published_identity: tuple[int, int] | None = None
    before_evidence: Path | None = None
    after_evidence: Path | None = None
    after_evidence_identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class SchedulerPaths:
    platform: str
    launchd_plist: Path | None = None
    systemd_service: Path | None = None
    systemd_timer: Path | None = None


def _display_path(path: Path) -> str:
    return str(path.expanduser())


def _validate_manifest_unicode_scalars(manifest: object) -> None:
    pending = [manifest]
    seen_containers: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise SyncError(
                    "manifest contains a string that is not valid UTF-8"
                ) from error
            continue
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend(value)


def _validate_relative_path(raw: object, field_name: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw:
        raise SyncError(f"{field_name} must be a non-empty relative path")
    if "\0" in raw:
        raise SyncError(f"{field_name} must not contain embedded NUL")
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise SyncError(f"{field_name} must be valid UTF-8") from error
    raw_parts = raw.split("/")
    if raw.startswith("/") or ".." in raw_parts:
        raise SyncError(f"{field_name} must not be absolute or contain parent traversal: {raw}")
    if any(part in ("", ".") for part in raw_parts):
        raise SyncError(f"{field_name} must not contain empty or current-dir segments: {raw}")
    path = PurePosixPath(raw)
    return path


def _validate_target_path(raw: object, field_name: str) -> PurePosixPath:
    path = _validate_relative_path(raw, field_name)
    encoded = path.as_posix().encode("utf-8")
    if len(encoded) > MAX_MANIFEST_TARGET_PATH_BYTES:
        raise SyncError(
            f"{field_name} exceeds {MAX_MANIFEST_TARGET_PATH_BYTES} UTF-8 bytes"
        )
    if len(path.parts) > MAX_MANIFEST_TARGET_PATH_DEPTH:
        raise SyncError(
            f"{field_name} exceeds {MAX_MANIFEST_TARGET_PATH_DEPTH} path components"
        )
    for index, part in enumerate(path.parts, start=1):
        component_bytes = len(part.encode("utf-8"))
        if component_bytes > MAX_MANIFEST_TARGET_COMPONENT_BYTES:
            raise SyncError(
                f"{field_name} component {index} exceeds "
                f"{MAX_MANIFEST_TARGET_COMPONENT_BYTES} UTF-8 bytes"
            )
    path_key = _portable_target_key(path)
    reserved_targets = (
        (SYNC_INTERNAL_TARGET, "sync internal path"),
        (
            PurePosixPath(PENDING_LINK_POINTER_NAME),
            "pending transaction pointer path",
        ),
    )
    for reserved_target, label in reserved_targets:
        reserved_key = _portable_target_key(reserved_target)
        if path_key[: len(reserved_key)] == reserved_key:
            raise SyncError(f"{field_name} must not use {label}: {path}")
    return path


def _validate_owner(raw: object, field_name: str = "owner") -> str:
    if raw is None:
        return PUBLIC_OWNER
    if not isinstance(raw, str) or not OWNER_RE.fullmatch(raw):
        raise SyncError(
            f"{field_name} must be a non-empty owner id containing only letters, "
            "numbers, '.', '_', or '-'"
        )
    return raw


def _validate_explicit_owner(raw: object, field_name: str = "owner") -> str:
    if raw is None:
        raise SyncError(
            f"{field_name} must be a non-empty owner id containing only letters, "
            "numbers, '.', '_', or '-'"
        )
    return _validate_owner(raw, field_name)


def _validate_release_sha(raw: object, field_name: str = "release SHA") -> str:
    if not isinstance(raw, str) or RELEASE_DIR_RE.fullmatch(raw) is None:
        raise SyncError(
            f"{field_name} must be 40 lowercase hex characters: {raw}"
        )
    return raw


def _validate_removed_link_key(raw: object, field_name: str) -> str:
    if not isinstance(raw, str):
        raise SyncError(f"{field_name} must be an owner:id string")
    owner, separator, removed_id = raw.partition(":")
    if (
        not separator
        or ":" in removed_id
        or REMOVED_LINK_ID_RE.fullmatch(removed_id) is None
    ):
        raise SyncError(f"{field_name} must be an owner:id string")
    return f"{_validate_owner(owner, field_name)}:{removed_id}"


def _portable_target_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in path.parts
    )


def _validate_portable_target_spellings(targets: list[PurePosixPath]) -> None:
    spellings: dict[tuple[str, ...], PurePosixPath] = {}
    for target in targets:
        key = _portable_target_key(target)
        previous = spellings.get(key)
        if previous is not None and previous != target:
            raise SyncError(
                f"portable target spellings conflict: {previous} and {target}"
            )
        spellings[key] = target


def _validate_non_overlapping_targets(targets: list[PurePosixPath]) -> None:
    _validate_portable_target_spellings(targets)
    unique_targets = list(dict.fromkeys(targets))
    ordered = sorted(
        (
            (_portable_target_key(path), path)
            for path in unique_targets
        ),
        key=lambda item: (item[0], item[1].as_posix()),
    )
    for (parent_key, parent), (child_key, child) in zip(ordered, ordered[1:]):
        if (
            len(parent_key) < len(child_key)
            and child_key[: len(parent_key)] == parent_key
        ):
            raise SyncError(
                f"manifest targets must not overlap: {parent} is an ancestor of {child}"
            )


def _validate_cross_owner_active_removed_target_hierarchy(
    manifests: list[ManifestData],
) -> None:
    class TargetNode:
        __slots__ = ("children", "descendants", "exact")

        def __init__(self) -> None:
            self.children: dict[str, TargetNode] = {}
            self.descendants: list[tuple[str, PurePosixPath]] = []
            self.exact: list[tuple[str, PurePosixPath]] = []

    def add_owner_representative(
        representatives: list[tuple[str, PurePosixPath]],
        owner: str,
        target: PurePosixPath,
    ) -> None:
        if any(
            candidate_owner == owner
            for candidate_owner, _target in representatives
        ):
            return
        # A lookup excludes only one owner, so two distinct representatives
        # are sufficient to prove whether another owner is present.
        if len(representatives) < 2:
            representatives.append((owner, target))

    def cross_owner_representative(
        representatives: list[tuple[str, PurePosixPath]],
        owner: str,
    ) -> tuple[str, PurePosixPath] | None:
        return next(
            (
                candidate
                for candidate in representatives
                if candidate[0] != owner
            ),
            None,
        )

    root = TargetNode()
    for manifest in manifests:
        for entry in manifest.entries:
            node = root
            for part in _portable_target_key(entry.target):
                add_owner_representative(
                    node.descendants,
                    manifest.owner,
                    entry.target,
                )
                child = node.children.get(part)
                if child is None:
                    child = TargetNode()
                    node.children[part] = child
                node = child
            add_owner_representative(
                node.exact,
                manifest.owner,
                entry.target,
            )

    for manifest in manifests:
        # A replacement target is an exact desired-target reference, not a
        # historical path that reconciliation mutates.
        for removed in manifest.removed_links:
            node = root
            conflict: tuple[str, PurePosixPath] | None = None
            for part in _portable_target_key(removed.target):
                conflict = cross_owner_representative(node.exact, manifest.owner)
                if conflict is not None:
                    break
                child = node.children.get(part)
                if child is None:
                    break
                node = child
            else:
                conflict = cross_owner_representative(
                    node.descendants,
                    manifest.owner,
                )
            if conflict is None:
                continue
            active_owner, active_target = conflict
            raise SyncError(
                "manifest active and removed targets must not overlap across owners: "
                f"{active_target} ({active_owner}) and "
                f"{removed.target} ({manifest.owner})"
            )


def _validate_manifest_target_portability(manifests: list[ManifestData]) -> None:
    active_targets: list[PurePosixPath] = []
    all_targets: list[PurePosixPath] = []
    for manifest in manifests:
        for entry in manifest.entries:
            active_targets.append(entry.target)
            all_targets.append(entry.target)
        for removed in manifest.removed_links:
            all_targets.append(removed.target)
            if removed.replacement_target is not None:
                all_targets.append(removed.replacement_target)
    _validate_portable_target_spellings(all_targets)
    _validate_non_overlapping_targets(active_targets)
    _validate_cross_owner_active_removed_target_hierarchy(manifests)


def _validate_install_target_portability(
    current_manifests: dict[str, ManifestData],
    next_manifests: dict[str, ManifestData],
) -> None:
    _validate_manifest_target_portability(
        [*current_manifests.values(), *next_manifests.values()]
    )


def _validate_planned_overlay_base_release_shas(
    next_manifests: dict[str, ManifestData],
    planned_public_sha: str | None,
) -> None:
    for owner, manifest in sorted(next_manifests.items()):
        required_public_sha = manifest.base_release_sha
        if owner == PUBLIC_OWNER or required_public_sha is None:
            continue
        if planned_public_sha is None:
            raise SyncError(
                f"overlay {owner} requires public release {required_public_sha}, "
                "but the planned install has no public release; rerun "
                "install-private with the required public base and overlay"
            )
        if required_public_sha != planned_public_sha:
            raise SyncError(
                f"overlay {owner} requires public release {required_public_sha}, "
                f"but the planned public release is {planned_public_sha}; rerun "
                "install-private with the required public base and overlay"
            )


def _manifest_path_kind(
    path_kind: Callable[[PurePosixPath], str | None],
    path: PurePosixPath,
    field_name: str,
) -> str | None:
    try:
        return path_kind(path)
    except (OSError, ValueError) as error:
        raise SyncError(f"{field_name} is not a valid filesystem path: {path}") from error


def _normalize_release(release: dict[str, Any]) -> dict[str, Any]:
    if "tagName" in release:
        return release
    if "tag_name" in release:
        return {
            "tagName": release.get("tag_name"),
            "targetCommitish": release.get("target_commitish"),
            "assets": release.get("assets", []),
        }
    return release


def _manifest_payload_digest(data: dict[str, Any]) -> str:
    try:
        canonical_payload = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ) as error:
        raise SyncError("sync manifest payload could not be canonicalized") from error
    return hashlib.sha256(canonical_payload).hexdigest()


def _parse_manifest_data(
    data: dict[str, Any],
    path_kind: Callable[[PurePosixPath], str | None],
) -> ManifestData:
    _validate_manifest_unicode_scalars(data)
    version = data.get("version")
    if type(version) is not int or version != 1:
        raise SyncError("sync manifest version must be 1")
    manifest_owner = _validate_explicit_owner(
        data.get("owner", PUBLIC_OWNER),
    )
    raw_links = data.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise SyncError("sync manifest must contain a non-empty links array")
    if len(raw_links) > MAX_MANIFEST_ACTIVE_LINKS:
        raise SyncError(
            "sync manifest active links exceed runtime transaction limit: "
            f"{len(raw_links)} > {MAX_MANIFEST_ACTIVE_LINKS}"
        )

    entries: list[LinkEntry] = []
    targets: set[PurePosixPath] = set()
    for index, raw_entry in enumerate(raw_links):
        if not isinstance(raw_entry, dict):
            raise SyncError(f"manifest link #{index + 1} must be an object")
        source = _validate_relative_path(raw_entry.get("source"), "source")
        target = _validate_target_path(raw_entry.get("target"), "target")
        kind = raw_entry.get("kind")
        if kind not in {"file", "directory", "skill"}:
            raise SyncError(f"manifest link {source} has unsupported kind: {kind}")
        owner = _validate_explicit_owner(
            raw_entry.get("owner", manifest_owner),
            "link owner",
        )
        if owner != manifest_owner:
            raise SyncError(
                f"manifest link {source} owner {owner} does not match manifest owner "
                f"{manifest_owner}"
            )
        override = raw_entry.get("override", False)
        if not isinstance(override, bool):
            raise SyncError(f"manifest link {source} override must be boolean")
        if owner == PUBLIC_OWNER and override:
            raise SyncError("public manifest links must not declare override=true")
        if target in targets:
            raise SyncError(f"duplicate manifest target: {target}")
        targets.add(target)
        source_type = _manifest_path_kind(path_kind, source, "source")
        if kind == "file":
            if source_type != "file":
                raise SyncError(f"manifest file source is missing: {source}")
        else:
            if source_type != "directory":
                raise SyncError(f"manifest directory source is missing: {source}")
            if kind == "skill" and _manifest_path_kind(
                path_kind,
                source / "SKILL.md",
                "skill source",
            ) != "file":
                raise SyncError(f"manifest skill source is missing SKILL.md: {source}")
        entries.append(
            LinkEntry(
                source=source,
                target=target,
                kind=kind,
                owner=owner,
                override=override,
            )
        )
    _validate_non_overlapping_targets([entry.target for entry in entries])

    raw_references = data.get("reference_only", [])
    if not isinstance(raw_references, list):
        raise SyncError("reference_only must be an array when present")
    for raw_reference in raw_references:
        reference = _validate_relative_path(raw_reference, "reference_only")
        if _manifest_path_kind(path_kind, reference, "reference_only") not in {
            "file",
            "directory",
        }:
            raise SyncError(f"reference_only path is missing: {reference}")

    raw_removed_links = data.get("removed_links", [])
    if not isinstance(raw_removed_links, list):
        raise SyncError("removed_links must be an array when present")
    removed_links: list[RemovedLink] = []
    removed_ids: set[str] = set()
    for index, raw_removed in enumerate(raw_removed_links):
        if not isinstance(raw_removed, dict):
            raise SyncError(f"removed link #{index + 1} must be an object")
        unknown_fields = sorted(set(raw_removed) - REMOVED_LINK_FIELDS)
        if unknown_fields:
            raise SyncError(
                f"removed link #{index + 1} has unsupported field(s): "
                + ", ".join(unknown_fields)
            )
        removed_id = raw_removed.get("id")
        if not isinstance(removed_id, str) or REMOVED_LINK_ID_RE.fullmatch(removed_id) is None:
            raise SyncError(
                "removed link id must contain only letters, numbers, '.', '_', or '-'"
            )
        if removed_id in removed_ids:
            raise SyncError(f"duplicate removed link id: {removed_id}")
        removed_ids.add(removed_id)
        source = _validate_relative_path(raw_removed.get("source"), "removed link source")
        target = _validate_target_path(raw_removed.get("target"), "removed link target")
        kind = raw_removed.get("kind")
        if kind not in {"file", "directory", "skill"}:
            raise SyncError(f"removed link {removed_id} has unsupported kind: {kind}")
        raw_replacement = raw_removed.get("replacement_target")
        replacement_target = (
            None
            if raw_replacement is None
            else _validate_target_path(raw_replacement, "replacement_target")
        )
        raw_retires = raw_removed.get("retires_replacements", [])
        if not isinstance(raw_retires, list):
            raise SyncError(
                f"removed link {removed_id} retires_replacements must be an array"
            )
        retires_replacements = tuple(
            _validate_removed_link_key(
                raw_key,
                f"removed link {removed_id} retires_replacements entry",
            )
            for raw_key in raw_retires
        )
        if len(set(retires_replacements)) != len(retires_replacements):
            raise SyncError(
                f"removed link {removed_id} has duplicate retires_replacements entries"
            )
        legacy = raw_removed.get("legacy", False)
        if not isinstance(legacy, bool):
            raise SyncError(f"removed link {removed_id} legacy must be boolean")
        removed_links.append(
            RemovedLink(
                id=removed_id,
                source=source,
                target=target,
                kind=kind,
                owner=manifest_owner,
                replacement_target=replacement_target,
                retires_replacements=retires_replacements,
                legacy=legacy,
            )
        )

    raw_base_release = data.get("base_release", {})
    if raw_base_release is None:
        raw_base_release = {}
    if not isinstance(raw_base_release, dict):
        raise SyncError("base_release must be an object when present")
    base_release_repo = raw_base_release.get("repo")
    if base_release_repo is not None and (
        not isinstance(base_release_repo, str)
        or REPOSITORY_RE.fullmatch(base_release_repo) is None
    ):
        raise SyncError("base_release.repo must be an owner/repo string")
    base_release_sha = raw_base_release.get("sha")
    if base_release_sha is not None and (
        not isinstance(base_release_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", base_release_sha) is None
    ):
        raise SyncError("base_release.sha must be a 40-character lowercase hex SHA")

    manifest = ManifestData(
        owner=manifest_owner,
        entries=entries,
        removed_links=removed_links,
        payload_digest=_manifest_payload_digest(data),
        base_release_repo=base_release_repo,
        base_release_sha=base_release_sha,
    )
    _validate_manifest_target_portability([manifest])
    return manifest


def load_manifest_data(release_root: Path) -> ManifestData:
    parent_fd, root_fd, root_snapshot = _open_release_source_root(release_root)
    try:
        _data, manifest = _load_manifest_data_from_directory_fd(root_fd, release_root)
        _require_release_source_unchanged(
            root_snapshot,
            os.fstat(root_fd),
            release_root,
        )
        current_root = os.stat(
            release_root.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        _require_release_source_unchanged(root_snapshot, current_root, release_root)
        return manifest
    except OSError as error:
        raise SyncError(f"release source root is unsafe: {release_root}") from error
    finally:
        _close_fd_quietly(root_fd)
        _close_fd_quietly(parent_fd)


def load_manifest(release_root: Path) -> list[LinkEntry]:
    return load_manifest_data(release_root).entries


def _load_base_release_spec(
    manifest: ManifestData,
    fallback_repo: str,
) -> BaseReleaseSpec:
    repo = manifest.base_release_repo or fallback_repo
    if not isinstance(repo, str) or REPOSITORY_RE.fullmatch(repo) is None:
        raise SyncError("base_release.repo must be an owner/repo string")
    return BaseReleaseSpec(repo=repo, sha=manifest.base_release_sha)


def _validated_release_asset_metadata(
    asset: dict[str, Any],
    asset_name: str,
    *,
    maximum_bytes: int,
) -> tuple[int, int]:
    asset_id = asset.get("id")
    if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0:
        raise SyncError(f"release asset {asset_name} has an invalid GitHub asset id")
    asset_size = asset.get("size")
    if (
        isinstance(asset_size, bool)
        or not isinstance(asset_size, int)
        or asset_size < 0
    ):
        raise SyncError(f"release asset {asset_name} has an invalid GitHub asset size")
    if asset_size > maximum_bytes:
        raise SyncError(
            f"release asset {asset_name} exceeds {maximum_bytes} byte limit"
        )
    return asset_id, asset_size


def select_release_assets(release: dict[str, Any]) -> ReleaseAssets:
    release = _normalize_release(release)
    tag_name = release.get("tagName")
    if not isinstance(tag_name, str) or not tag_name.startswith(TAG_PREFIX):
        raise SyncError("release tag is not a personal Codex release")
    tag_match = TAG_RE.fullmatch(tag_name)
    if tag_match is None:
        raise SyncError(f"release tag does not match personal Codex format: {tag_name}")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise SyncError("release assets must be an array")

    archive_matches: list[tuple[str, dict[str, Any]]] = []
    checksum_matches: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        archive_match = ASSET_RE.fullmatch(name)
        if archive_match:
            archive_matches.append((archive_match.group(1), asset))
            continue
        checksum_match = SHA256_RE.fullmatch(name)
        if checksum_match:
            checksum_matches.setdefault(checksum_match.group(1), []).append(asset)

    if not archive_matches:
        raise SyncError(f"release {tag_name} has no personal-codex tarball asset")
    if len(archive_matches) > 1:
        names = ", ".join(str(asset.get("name")) for _, asset in archive_matches)
        raise SyncError(f"release {tag_name} has multiple tarball assets: {names}")
    sha, archive_asset = archive_matches[0]
    archive_name = archive_asset["name"]
    matching_checksums = checksum_matches.get(sha, [])
    if not matching_checksums:
        raise SyncError(f"release {tag_name} is missing checksum asset for {archive_name}")
    if len(matching_checksums) > 1:
        raise SyncError(
            f"release {tag_name} has multiple checksum assets for {archive_name}"
        )
    checksum_asset = matching_checksums[0]
    checksum_name = checksum_asset["name"]
    tag_short_sha = tag_match.group(1)
    if not sha.startswith(tag_short_sha):
        raise SyncError(
            f"release asset SHA {sha} does not match tag suffix {tag_short_sha}"
        )
    target_commitish = release.get("targetCommitish")
    if (
        isinstance(target_commitish, str)
        and re.fullmatch(r"[0-9a-f]{40}", target_commitish)
        and target_commitish != sha
    ):
        raise SyncError(
            f"release asset SHA {sha} does not match target commit {target_commitish}"
        )
    archive_id, archive_size = _validated_release_asset_metadata(
        archive_asset,
        archive_name,
        maximum_bytes=MAX_ARCHIVE_COMPRESSED_BYTES,
    )
    checksum_id, checksum_size = _validated_release_asset_metadata(
        checksum_asset,
        checksum_name,
        maximum_bytes=MAX_ARCHIVE_CHECKSUM_BYTES,
    )
    if archive_id == checksum_id:
        raise SyncError("release archive and checksum must have distinct GitHub asset ids")
    return ReleaseAssets(
        tag_name=tag_name,
        sha=sha,
        archive_name=archive_name,
        archive_id=archive_id,
        archive_size=archive_size,
        checksum_name=checksum_name,
        checksum_id=checksum_id,
        checksum_size=checksum_size,
    )


def _release_mentions_asset_sha(release: dict[str, Any], sha: str) -> bool:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return False
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        archive_match = ASSET_RE.fullmatch(name)
        if archive_match and archive_match.group(1) == sha:
            return True
        checksum_match = SHA256_RE.fullmatch(name)
        if checksum_match and checksum_match.group(1) == sha:
            return True
    return False


def _regular_file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_bounded_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    description: str,
) -> tuple[int, int, os.stat_result]:
    if path.name in {"", ".", ".."}:
        raise SyncError(f"refusing unsafe {description} path: {path}")
    try:
        parent_fd = os.open(path.parent, _archive_directory_open_flags())
    except OSError as error:
        raise SyncError(f"refusing unsafe {description} parent: {path.parent}") from error
    file_descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        file_descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SyncError(f"refusing non-regular {description}: {path}")
        if metadata.st_size > maximum_bytes:
            raise SyncError(
                f"{description} exceeds {maximum_bytes} byte limit: {path}"
            )
        if (
            not _archive_path_matches_fd(path.parent, parent_fd)
            or not _archive_entry_matches_fd(parent_fd, path.name, file_descriptor)
            or not _archive_path_matches_fd(path, file_descriptor)
        ):
            raise SyncError(f"{description} changed while opening: {path}")
        return parent_fd, file_descriptor, metadata
    except OSError as error:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(parent_fd)
        raise SyncError(f"refusing unsafe {description}: {path}") from error
    except BaseException:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(parent_fd)
        raise


def _require_bounded_regular_file_unchanged(
    path: Path,
    parent_fd: int,
    file_descriptor: int,
    initial_metadata: os.stat_result,
    description: str,
) -> None:
    current_metadata = os.fstat(file_descriptor)
    if (
        _regular_file_snapshot(current_metadata)
        != _regular_file_snapshot(initial_metadata)
        or not stat.S_ISREG(current_metadata.st_mode)
        or not _archive_path_matches_fd(path.parent, parent_fd)
        or not _archive_entry_matches_fd(parent_fd, path.name, file_descriptor)
        or not _archive_path_matches_fd(path, file_descriptor)
    ):
        raise SyncError(f"{description} changed while reading: {path}")


def _read_bounded_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    description: str,
) -> bytes:
    parent_fd, file_descriptor, metadata = _open_bounded_regular_file(
        path,
        maximum_bytes=maximum_bytes,
        description=description,
    )
    try:
        payload = bytearray()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise SyncError(f"{description} ended early: {path}")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(file_descriptor, 1):
            raise SyncError(f"{description} grew while reading: {path}")
        _require_bounded_regular_file_unchanged(
            path,
            parent_fd,
            file_descriptor,
            metadata,
            description,
        )
        return bytes(payload)
    finally:
        os.close(file_descriptor)
        os.close(parent_fd)


def _copy_archive_to_immutable_snapshot(
    archive_path: Path,
) -> tuple[Any, str]:
    parent_fd, archive_fd, metadata = _open_bounded_regular_file(
        archive_path,
        maximum_bytes=MAX_ARCHIVE_COMPRESSED_BYTES,
        description="compressed archive",
    )
    snapshot: Any | None = None
    digest = hashlib.sha256()
    try:
        snapshot = tempfile.TemporaryFile(mode="w+b")
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(archive_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SyncError(f"compressed archive ended early: {archive_path}")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = snapshot.write(view)
                if written is None or written <= 0:
                    raise SyncError("failed to write immutable archive snapshot")
                view = view[written:]
            remaining -= len(chunk)
        if os.read(archive_fd, 1):
            raise SyncError(f"compressed archive grew while reading: {archive_path}")
        _require_bounded_regular_file_unchanged(
            archive_path,
            parent_fd,
            archive_fd,
            metadata,
            "compressed archive",
        )
        snapshot.flush()
        os.fsync(snapshot.fileno())
        snapshot.seek(0)
        return snapshot, digest.hexdigest()
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        raise
    finally:
        os.close(archive_fd)
        os.close(parent_fd)


def _expected_archive_checksum(archive_path: Path, checksum_path: Path) -> str:
    checksum_payload = _read_bounded_regular_file(
        checksum_path,
        maximum_bytes=MAX_ARCHIVE_CHECKSUM_BYTES,
        description="checksum file",
    )
    try:
        checksum_text = checksum_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SyncError(f"checksum file is not valid UTF-8: {checksum_path}") from error
    expected: str | None = None
    archive_name = archive_path.name
    for line in checksum_text.splitlines():
        fields = line.strip().split()
        if not fields:
            continue
        checksum_target = Path(fields[-1].lstrip("*")).name if len(fields) > 1 else archive_name
        if checksum_target == archive_name:
            candidate = fields[0]
            if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
                expected = candidate.lower()
                break
    if expected is None:
        raise SyncError(f"checksum file does not contain a sha256 for {archive_name}")
    return expected


def _verified_archive_snapshot(archive_path: Path, checksum_path: Path) -> Any:
    expected = _expected_archive_checksum(archive_path, checksum_path)
    snapshot, actual = _copy_archive_to_immutable_snapshot(archive_path)
    if actual != expected:
        snapshot.close()
        raise SyncError(
            f"checksum mismatch for {archive_path.name}: expected {expected}, got {actual}"
        )
    return snapshot


def verify_checksum(archive_path: Path, checksum_path: Path) -> None:
    snapshot = _verified_archive_snapshot(archive_path, checksum_path)
    snapshot.close()


def _validated_archive_member_parts(member_name: str) -> tuple[str, ...]:
    if len(member_name) > MAX_ARCHIVE_MEMBER_PATH_BYTES:
        raise SyncError(
            "archive member path exceeds UTF-8 byte limit: "
            f"> {MAX_ARCHIVE_MEMBER_PATH_BYTES}"
        )
    if "\0" in member_name:
        raise SyncError("refusing unsafe archive member path: embedded NUL")
    try:
        encoded_name = member_name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SyncError("archive member path is not valid UTF-8") from error
    if len(encoded_name) > MAX_ARCHIVE_MEMBER_PATH_BYTES:
        raise SyncError(
            "archive member path exceeds UTF-8 byte limit: "
            f"{len(encoded_name)} > {MAX_ARCHIVE_MEMBER_PATH_BYTES}"
        )

    raw_parts = tuple(member_name.split("/"))
    if member_name.startswith("/") or any(
        part in {"", ".", ".."} for part in raw_parts
    ):
        raise SyncError(f"refusing unsafe archive member path: {member_name}")
    if len(raw_parts) > MAX_ARCHIVE_MEMBER_PATH_DEPTH:
        raise SyncError(
            "archive member path exceeds depth limit: "
            f"{len(raw_parts)} > {MAX_ARCHIVE_MEMBER_PATH_DEPTH}"
        )
    for index, part in enumerate(raw_parts, start=1):
        encoded_part = part.encode("utf-8")
        if len(encoded_part) > MAX_ARCHIVE_MEMBER_COMPONENT_BYTES:
            raise SyncError(
                "archive member path component exceeds UTF-8 byte limit: "
                f"component {index}: {len(encoded_part)} > "
                f"{MAX_ARCHIVE_MEMBER_COMPONENT_BYTES}"
            )
    return raw_parts


def _validate_tar_member(member: tarfile.TarInfo) -> None:
    member_name = member.name
    if member.isdir() and member_name.endswith("/"):
        member_name = member_name[:-1]
    _validated_archive_member_parts(member_name)
    member.name = member_name
    if member.issym() or member.islnk():
        raise SyncError(f"refusing archive link member: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise SyncError(f"refusing unsupported archive member type: {member.name}")
    # Releases are installed under a single user's home; keep executables usable
    # while stripping special and group/world-write bits during extraction.
    if member.isdir():
        member.mode = (member.mode & 0o755) | 0o700
    else:
        member.mode &= 0o755


class _ArchivePathTrieNode:
    __slots__ = ("children", "kind", "original_component", "parent")

    def __init__(
        self,
        *,
        parent: _ArchivePathTrieNode | None = None,
        original_component: str | None = None,
    ) -> None:
        self.children: dict[str, _ArchivePathTrieNode] = {}
        self.kind: str | None = None
        self.original_component = original_component
        self.parent = parent


def _archive_trie_node_path(node: _ArchivePathTrieNode) -> str:
    parts: list[str] = []
    current: _ArchivePathTrieNode | None = node
    while current is not None and current.original_component is not None:
        parts.append(current.original_component)
        current = current.parent
    return "/".join(reversed(parts))


def _validate_archive_member_paths(members: list[tarfile.TarInfo]) -> None:
    explicit_paths: set[str] = set()
    portable_root = _ArchivePathTrieNode()
    path_entry_count = 0

    for member in members:
        if member.name in explicit_paths:
            raise SyncError(
                "duplicate archive member path: "
                f"{member.name} and {member.name}"
            )
        explicit_paths.add(member.name)
        parts = tuple(member.name.split("/"))
        node = portable_root
        for index, part in enumerate(parts):
            portable_component = unicodedata.normalize("NFC", part).casefold()
            child = node.children.get(portable_component)
            if child is None:
                if path_entry_count >= MAX_ARCHIVE_MEMBERS:
                    raise SyncError(
                        f"archive exceeds {MAX_ARCHIVE_MEMBERS} path entry limit"
                    )
                child = _ArchivePathTrieNode(
                    parent=node,
                    original_component=part,
                )
                node.children[portable_component] = child
                path_entry_count += 1
            desired_kind = (
                "directory"
                if index < len(parts) - 1 or member.isdir()
                else "file"
            )
            if child.original_component != part or (
                child.kind is not None and child.kind != desired_kind
            ):
                previous_kind = child.kind or "directory"
                current_path = "/".join(parts[: index + 1])
                raise SyncError(
                    "portable archive member path conflict: "
                    f"{_archive_trie_node_path(child)} ({previous_kind}) "
                    f"conflicts with {current_path} ({desired_kind})"
                )
            child.kind = desired_kind
            node = child


def _archive_path_matches_fd(path: Path, file_descriptor: int) -> bool:
    try:
        bound_metadata = os.fstat(file_descriptor)
        current_metadata = os.lstat(path)
    except OSError:
        return False
    return (
        (bound_metadata.st_dev, bound_metadata.st_ino)
        == (current_metadata.st_dev, current_metadata.st_ino)
    )


def _archive_entry_matches_fd(
    parent_fd: int,
    name: str,
    file_descriptor: int,
) -> bool:
    try:
        bound_metadata = os.fstat(file_descriptor)
        current_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        (bound_metadata.st_dev, bound_metadata.st_ino)
        == (current_metadata.st_dev, current_metadata.st_ino)
    )


def _temporary_archive_entry_names(kind: str):
    for _attempt in range(128):
        yield f".codex-extract-{kind}-{os.getpid()}-{os.urandom(8).hex()}"
    raise SyncError("failed to allocate a temporary archive entry")


def _archive_directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _create_archive_directory_at(parent_fd: int, name: str) -> int:
    temporary_name: str | None = None
    directory_fd: int | None = None
    try:
        for candidate in _temporary_archive_entry_names("dir"):
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None:
            raise SyncError("failed to allocate a temporary archive directory")
        created_metadata = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        directory_fd = os.open(
            temporary_name,
            _archive_directory_open_flags(),
            dir_fd=parent_fd,
        )
        bound_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(created_metadata.st_mode)
            or (created_metadata.st_dev, created_metadata.st_ino)
            != (bound_metadata.st_dev, bound_metadata.st_ino)
        ):
            raise SyncError(f"temporary archive directory changed: {name}")
        os.fchmod(directory_fd, 0o700)
        try:
            _rename_noreplace_at(parent_fd, temporary_name, parent_fd, name)
        except FileExistsError as error:
            raise _ArchiveEntryExistsError(
                f"archive entry already exists: {name}"
            ) from error
        temporary_name = None
        if not _archive_entry_matches_fd(parent_fd, name, directory_fd):
            raise SyncError(f"archive directory changed during publication: {name}")
        return directory_fd
    except BaseException:
        if directory_fd is not None:
            os.close(directory_fd)
        if temporary_name is not None:
            try:
                os.rmdir(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise


def _create_archive_destination(destination: Path) -> tuple[int, int]:
    if destination.name in {"", ".", ".."}:
        raise SyncError(f"refusing unsafe archive destination: {destination}")
    try:
        parent_fd = os.open(destination.parent, _archive_directory_open_flags())
    except OSError as error:
        raise SyncError(
            f"refusing unsafe archive destination parent: {destination.parent}"
        ) from error
    destination_fd: int | None = None
    try:
        try:
            destination_fd = _create_archive_directory_at(parent_fd, destination.name)
        except _ArchiveEntryExistsError as error:
            raise SyncError(
                f"refusing pre-existing archive destination: {destination}"
            ) from error
        if not _archive_path_matches_fd(destination.parent, parent_fd):
            raise SyncError(f"archive destination parent changed: {destination.parent}")
        if not _archive_entry_matches_fd(
            parent_fd,
            destination.name,
            destination_fd,
        ) or not _archive_path_matches_fd(destination, destination_fd):
            raise SyncError(f"archive destination changed after creation: {destination}")
        return parent_fd, destination_fd
    except BaseException:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(parent_fd)
        raise


def _open_archive_directory(
    destination_fd: int,
    directory_identities: dict[tuple[str, ...], tuple[int, int]],
    parts: tuple[str, ...],
) -> int:
    try:
        current_fd = os.dup(destination_fd)
    except OSError as error:
        raise SyncError("failed to bind archive destination") from error
    try:
        if _directory_identity(current_fd) != directory_identities[()]:
            raise SyncError("archive destination changed during extraction")
        for length in range(1, len(parts) + 1):
            current_parts = parts[:length]
            expected_identity = directory_identities.get(current_parts)
            if expected_identity is None:
                raise SyncError("archive directory is missing during extraction")
            next_fd = os.open(
                current_parts[-1],
                _archive_directory_open_flags(),
                dir_fd=current_fd,
            )
            try:
                if (
                    _directory_identity(next_fd) != expected_identity
                    or not _archive_entry_matches_fd(
                        current_fd,
                        current_parts[-1],
                        next_fd,
                    )
                ):
                    raise SyncError("archive directory changed during extraction")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except SyncError:
        os.close(current_fd)
        raise
    except OSError as error:
        os.close(current_fd)
        raise SyncError("archive directory changed during extraction") from error
    except BaseException:
        os.close(current_fd)
        raise


def _ensure_archive_directories(
    destination_fd: int,
    directory_identities: dict[tuple[str, ...], tuple[int, int]],
    parts: tuple[str, ...],
) -> int:
    try:
        current_fd = os.dup(destination_fd)
    except OSError as error:
        raise SyncError("failed to bind archive destination") from error
    try:
        if _directory_identity(current_fd) != directory_identities[()]:
            raise SyncError("archive destination changed during extraction")
        for length in range(1, len(parts) + 1):
            current_parts = parts[:length]
            expected_identity = directory_identities.get(current_parts)
            next_fd: int | None = None
            try:
                if expected_identity is None:
                    next_fd = _create_archive_directory_at(
                        current_fd,
                        current_parts[-1],
                    )
                    expected_identity = _directory_identity(next_fd)
                    directory_identities[current_parts] = expected_identity
                else:
                    next_fd = os.open(
                        current_parts[-1],
                        _archive_directory_open_flags(),
                        dir_fd=current_fd,
                    )
                    if (
                        _directory_identity(next_fd) != expected_identity
                        or not _archive_entry_matches_fd(
                            current_fd,
                            current_parts[-1],
                            next_fd,
                        )
                    ):
                        raise SyncError(
                            "archive directory changed during extraction"
                        )
            except BaseException:
                if next_fd is not None:
                    os.close(next_fd)
                raise
            if next_fd is None:
                raise SyncError("failed to open archive directory")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except SyncError:
        os.close(current_fd)
        raise
    except OSError as error:
        os.close(current_fd)
        raise SyncError("archive directory changed during extraction") from error
    except BaseException:
        os.close(current_fd)
        raise


def _write_archive_member_file(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    parent_fd: int,
    name: str,
) -> tuple[int, ...]:
    source = archive.extractfile(member)
    if source is None:
        raise SyncError(f"failed to read archive member: {member.name}")
    temporary_name: str | None = None
    file_descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        for candidate in _temporary_archive_entry_names("file"):
            try:
                file_descriptor = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None or file_descriptor is None:
            raise SyncError("failed to allocate a temporary archive file")

        created_metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(created_metadata.st_mode):
            raise SyncError(f"temporary archive file is not regular: {member.name}")
        remaining = member.size
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise SyncError(f"archive member ended early: {member.name}")
            view = memoryview(chunk)
            while view:
                written = os.write(file_descriptor, view)
                if written <= 0:
                    raise SyncError(f"failed to write archive member: {member.name}")
                view = view[written:]
            remaining -= len(chunk)
        os.fchmod(file_descriptor, member.mode)
        os.fsync(file_descriptor)
        metadata = os.fstat(file_descriptor)
        if (
            (created_metadata.st_dev, created_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != member.size
        ):
            raise SyncError(f"temporary archive file changed: {member.name}")
        try:
            _rename_noreplace_at(parent_fd, temporary_name, parent_fd, name)
        except FileExistsError as error:
            raise _ArchiveEntryExistsError(
                f"archive entry already exists: {member.name}"
            ) from error
        temporary_name = None
        if not _archive_entry_matches_fd(parent_fd, name, file_descriptor):
            raise SyncError(f"archive file changed during publication: {member.name}")
        return _regular_file_snapshot(os.fstat(file_descriptor))
    finally:
        source.close()
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass


def _archive_member_signature(member: tarfile.TarInfo) -> tuple[Any, ...]:
    return (
        member.name,
        "directory" if member.isdir() else "file",
        member.size,
        member.mode,
    )


class _BoundedDecompressedReader:
    def __init__(self, reader: Any, maximum_bytes: int) -> None:
        self._reader = reader
        self._maximum_bytes = maximum_bytes
        self._bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._maximum_bytes - self._bytes_read
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        payload = self._reader.read(requested)
        self._bytes_read += len(payload)
        if self._bytes_read > self._maximum_bytes:
            raise SyncError(
                "archive exceeds total expanded byte limit: "
                f"> {self._maximum_bytes}"
            )
        return payload


@contextlib.contextmanager
def _stream_archive_snapshot(snapshot: Any):
    snapshot.seek(0)
    try:
        with gzip.GzipFile(fileobj=snapshot, mode="rb") as decompressor:
            bounded_reader = _BoundedDecompressedReader(
                decompressor,
                MAX_ARCHIVE_EXPANDED_BYTES,
            )
            with tarfile.open(fileobj=bounded_reader, mode="r|") as archive:
                yield archive
            while bounded_reader.read(1024 * 1024):
                pass
    finally:
        snapshot.seek(0)


def _scan_archive_snapshot(
    snapshot: Any,
) -> tuple[list[tarfile.TarInfo], tuple[str, ...], tuple[str, ...]]:
    members: list[tarfile.TarInfo] = []
    expanded_bytes = 0
    try:
        with _stream_archive_snapshot(snapshot) as archive:
            for member in archive:
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    raise SyncError(
                        f"archive exceeds {MAX_ARCHIVE_MEMBERS} member limit"
                    )
                _validate_tar_member(member)
                if member.isfile():
                    if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise SyncError(
                            "archive member exceeds expanded byte limit: "
                            f"{member.name}: {member.size} > {MAX_ARCHIVE_MEMBER_BYTES}"
                        )
                    expanded_bytes += member.size
                    if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise SyncError(
                            "archive exceeds total expanded byte limit: "
                            f"{expanded_bytes} > {MAX_ARCHIVE_EXPANDED_BYTES}"
                        )
                members.append(member)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as error:
        raise SyncError(f"failed to inspect archive snapshot: {error}") from error
    if not members:
        raise SyncError("archive is empty")
    _validate_archive_member_paths(members)
    release_root_parts, manifest_parts = _archive_release_root_parts(members)
    return members, release_root_parts, manifest_parts


def _extract_archive_members(
    snapshot: Any,
    destination_fd: int,
    planned_members: list[tarfile.TarInfo],
) -> tuple[
    dict[tuple[str, ...], tuple[int, int]],
    dict[tuple[str, ...], tuple[int, ...]],
]:
    directory_identities = {(): _directory_identity(destination_fd)}
    file_identities: dict[tuple[str, ...], tuple[int, ...]] = {}
    directory_modes: dict[tuple[str, ...], int] = {}
    try:
        with _stream_archive_snapshot(snapshot) as archive:
            for expected_member in planned_members:
                member = archive.next()
                if member is None:
                    raise SyncError("archive snapshot ended before all planned members")
                _validate_tar_member(member)
                if _archive_member_signature(member) != _archive_member_signature(
                    expected_member
                ):
                    raise SyncError("archive snapshot metadata changed between passes")
                member_parts = PurePosixPath(member.name).parts
                if member.isdir():
                    directory_fd = _ensure_archive_directories(
                        destination_fd,
                        directory_identities,
                        member_parts,
                    )
                    try:
                        directory_modes[member_parts] = member.mode
                    finally:
                        os.close(directory_fd)
                else:
                    parent_fd = _ensure_archive_directories(
                        destination_fd,
                        directory_identities,
                        member_parts[:-1],
                    )
                    try:
                        file_identities[member_parts] = _write_archive_member_file(
                            archive,
                            member,
                            parent_fd,
                            member_parts[-1],
                        )
                    finally:
                        os.close(parent_fd)
            if archive.next() is not None:
                raise SyncError("archive snapshot gained members between passes")
        for directory_parts, mode in sorted(
            directory_modes.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            directory_fd = _open_archive_directory(
                destination_fd,
                directory_identities,
                directory_parts,
            )
            try:
                os.fchmod(directory_fd, mode)
            finally:
                os.close(directory_fd)
        return directory_identities, file_identities
    except (tarfile.TarError, OSError, EOFError, zlib.error) as error:
        raise SyncError(f"failed to extract archive snapshot: {error}") from error


def _archive_release_root_parts(
    members: list[tarfile.TarInfo],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    manifest_parts = MANIFEST_RELATIVE_PATH.parts
    candidates: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for member in members:
        if not member.isfile():
            continue
        member_parts = PurePosixPath(member.name).parts
        if member_parts == manifest_parts:
            candidates.append(((), member_parts))
        elif (
            len(member_parts) == len(manifest_parts) + 1
            and member_parts[1:] == manifest_parts
        ):
            candidates.append((member_parts[:1], member_parts))
    if len(candidates) != 1:
        raise SyncError("archive must contain exactly one release root with sync manifest")
    return candidates[0]


def _validate_archive_tree_evidence(
    destination_fd: int,
    directory_identities: dict[tuple[str, ...], tuple[int, int]],
    file_identities: dict[tuple[str, ...], tuple[int, ...]],
    manifest_parts: tuple[str, ...],
) -> None:
    expected_children: dict[tuple[str, ...], set[str]] = {
        parts: set() for parts in directory_identities
    }
    for directory_parts in directory_identities:
        if directory_parts:
            expected_children[directory_parts[:-1]].add(directory_parts[-1])
    for file_parts in file_identities:
        expected_children[file_parts[:-1]].add(file_parts[-1])

    for directory_parts in sorted(directory_identities, key=len):
        directory_fd = _open_archive_directory(
            destination_fd,
            directory_identities,
            directory_parts,
        )
        try:
            metadata = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or _directory_identity(directory_fd)
                != directory_identities[directory_parts]
            ):
                raise SyncError("archive directory changed during validation")
            with os.scandir(directory_fd) as entries:
                actual_children = {entry.name for entry in entries}
            if actual_children != expected_children[directory_parts]:
                raise SyncError("archive directory entries changed during validation")
        finally:
            os.close(directory_fd)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    for file_parts, expected_identity in file_identities.items():
        parent_fd = _open_archive_directory(
            destination_fd,
            directory_identities,
            file_parts[:-1],
        )
        try:
            file_descriptor = os.open(
                file_parts[-1],
                flags,
                dir_fd=parent_fd,
            )
        except OSError as error:
            os.close(parent_fd)
            raise SyncError("archive file changed during validation") from error
        try:
            metadata = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _regular_file_snapshot(metadata) != expected_identity
            ):
                raise SyncError("archive file changed during validation")
        finally:
            os.close(file_descriptor)
            os.close(parent_fd)

    if manifest_parts not in file_identities:
        raise SyncError("archive release manifest was not extracted")


def _safe_extract_archive_snapshot(
    snapshot: Any,
    destination: Path,
) -> tuple[Path, ReleaseTreeExpectation]:
    planned_members, release_root_parts, manifest_parts = _scan_archive_snapshot(
        snapshot
    )
    parent_fd, destination_fd = _create_archive_destination(destination)
    directory_identities = {(): _directory_identity(destination_fd)}
    release_fd: int | None = None
    try:
        directory_identities, file_identities = _extract_archive_members(
            snapshot,
            destination_fd,
            planned_members,
        )
        _validate_archive_tree_evidence(
            destination_fd,
            directory_identities,
            file_identities,
            manifest_parts,
        )
        release_root = destination.joinpath(*release_root_parts)
        release_fd = _open_archive_directory(
            destination_fd,
            directory_identities,
            release_root_parts,
        )
        release_directory_identity = _directory_identity(release_fd)
        initial_release_identity = _release_tree_identity_from_directory_fd(
            release_fd,
            release_root,
            require_sanitized_modes=True,
        )
        _validate_archive_tree_evidence(
            destination_fd,
            directory_identities,
            file_identities,
            manifest_parts,
        )
        final_release_identity = _release_tree_identity_from_directory_fd(
            release_fd,
            release_root,
            require_sanitized_modes=True,
        )
        if final_release_identity != initial_release_identity:
            raise SyncError("archive release content changed during final validation")
        if _directory_identity(release_fd) != release_directory_identity:
            raise SyncError("archive release root changed during final validation")
        _validate_archive_tree_evidence(
            destination_fd,
            directory_identities,
            file_identities,
            manifest_parts,
        )
        if (
            not _archive_path_matches_fd(destination.parent, parent_fd)
            or not _archive_entry_matches_fd(
                parent_fd,
                destination.name,
                destination_fd,
            )
            or not _archive_path_matches_fd(destination, destination_fd)
        ):
            raise SyncError(f"archive destination changed during extraction: {destination}")
        return release_root, (
            final_release_identity,
            release_directory_identity,
        )
    finally:
        if release_fd is not None:
            os.close(release_fd)
        os.close(destination_fd)
        os.close(parent_fd)


def safe_extract_archive(archive_path: Path, destination: Path) -> Path:
    snapshot, _digest = _copy_archive_to_immutable_snapshot(archive_path)
    try:
        release_root, _release_expectation = _safe_extract_archive_snapshot(
            snapshot,
            destination,
        )
        return release_root
    finally:
        snapshot.close()


def verify_and_extract_archive(
    archive_path: Path,
    checksum_path: Path,
    destination: Path,
) -> tuple[Path, ReleaseTreeExpectation]:
    snapshot = _verified_archive_snapshot(archive_path, checksum_path)
    try:
        return _safe_extract_archive_snapshot(snapshot, destination)
    finally:
        snapshot.close()


def find_release_root(extract_root: Path) -> Path:
    if (extract_root / MANIFEST_RELATIVE_PATH).is_file():
        return extract_root
    candidates = [
        child
        for child in extract_root.iterdir()
        if child.is_dir() and (child / MANIFEST_RELATIVE_PATH).is_file()
    ]
    if len(candidates) != 1:
        raise SyncError("archive must contain exactly one release root with sync manifest")
    return candidates[0]


def _personal_sync_root(home: Path) -> Path:
    return home / "personal-sync"


def _owner_sync_root(home: Path, owner: str) -> Path:
    sync_root = _personal_sync_root(home)
    if owner == PUBLIC_OWNER:
        return sync_root
    return sync_root / "overlays" / owner


def _releases_root(home: Path, owner: str = PUBLIC_OWNER) -> Path:
    return _owner_sync_root(home, owner) / "releases"


def _current_link(home: Path, owner: str = PUBLIC_OWNER) -> Path:
    return _owner_sync_root(home, owner) / "current"


def _install_lock_path(home: Path) -> Path:
    return _personal_sync_root(home) / "install.lock"


def _state_path(home: Path) -> Path:
    return _personal_sync_root(home) / STATE_RELATIVE_PATH


def _pending_link_pointer_path(home: Path) -> Path:
    # Keep the WAL pointer on the same stable home inode used by
    # ``installation_lock``.  Every directory below home may be atomically
    # rotated during recovery; placing the only pointer inside one of them
    # would let that rotation hide an unfinished transaction.
    return home / PENDING_LINK_POINTER_NAME


def _internal_path_parts(home: Path, path: Path) -> tuple[str, ...]:
    try:
        parts = _directory_parts_beneath(home, path)
    except SyncError as error:
        raise SyncError(f"sync internal path is outside home: {path}") from error
    if not parts or parts[0] != "personal-sync":
        raise SyncError(f"path is not inside personal-sync: {path}")
    return parts


def _ensure_safe_internal_directory(
    home: Path,
    path: Path,
    *,
    create: bool,
    allow_missing: bool = False,
) -> bool:
    """Validate every personal-sync directory component without following symlinks."""
    parts = _internal_path_parts(home, path)
    if create:
        home_fd = _open_or_create_sync_home(home)
        try:
            directory_fd = _open_or_create_directory_beneath(
                home,
                path,
                mode=0o700,
                home_fd=home_fd,
            )
            try:
                if not _bound_directory_matches(home, path, directory_fd):
                    raise SyncError(f"sync internal directory changed: {path}")
            finally:
                _close_fd_quietly(directory_fd)
        finally:
            _close_fd_quietly(home_fd)
        return True
    current = home
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if not create:
                if allow_missing:
                    return False
                raise SyncError(f"sync internal directory is missing: {current}")
            try:
                os.mkdir(current, mode=0o700)
            except FileExistsError:
                pass
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError as error:
                raise SyncError(
                    f"sync internal directory changed while creating it: {current}"
                ) from error
        if stat.S_ISLNK(mode):
            raise SyncError(f"refusing symlinked sync internal directory: {current}")
        if not stat.S_ISDIR(mode):
            raise SyncError(f"sync internal path is not a directory: {current}")
    return True


def _ensure_safe_internal_parent(
    home: Path,
    path: Path,
    *,
    create: bool,
    allow_missing: bool = False,
) -> bool:
    _internal_path_parts(home, path)
    return _ensure_safe_internal_directory(
        home,
        path.parent,
        create=create,
        allow_missing=allow_missing,
    )


def _directory_parts_beneath(home: Path, directory: Path) -> tuple[str, ...]:
    if os.fspath(directory) in {"", "."}:
        raise SyncError(f"refusing unsafe directory path component: {directory}")
    if home.is_absolute() != directory.is_absolute():
        raise SyncError(f"directory is outside sync home: {directory}")
    try:
        relative = directory.relative_to(home)
    except ValueError as error:
        raise SyncError(f"directory is outside sync home: {directory}") from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise SyncError(f"refusing unsafe directory path component: {directory}")
    return relative.parts


def _directory_open_flags(*, nofollow: bool) -> int:
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    root_flags |= getattr(os, "O_CLOEXEC", 0)
    if nofollow:
        root_flags |= getattr(os, "O_NOFOLLOW", 0)
    return root_flags


def _open_sync_home_parent(home: Path) -> int:
    parent = home.parent
    try:
        parent_fd = os.open(parent, _directory_open_flags(nofollow=True))
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SyncError(f"refusing unsafe sync home parent: {parent}") from error
    if not _archive_path_matches_fd(parent, parent_fd):
        _close_fd_quietly(parent_fd)
        raise SyncError(f"sync home parent changed while opening it: {parent}")
    return parent_fd


def _sync_home_matches_fd(
    home: Path,
    home_fd: int,
    *,
    parent_fd: int | None = None,
) -> bool:
    try:
        _directory_identity(home_fd)
    except (OSError, SyncError):
        return False
    if home.is_absolute() and home.parent == home:
        return _archive_path_matches_fd(home, home_fd)
    owns_parent_fd = parent_fd is None
    if parent_fd is None:
        try:
            parent_fd = _open_sync_home_parent(home)
        except (OSError, SyncError):
            return False
    try:
        return (
            _archive_path_matches_fd(home.parent, parent_fd)
            and _archive_entry_matches_fd(parent_fd, home.name, home_fd)
            and _archive_path_matches_fd(home.parent, parent_fd)
        )
    finally:
        if owns_parent_fd:
            _close_fd_quietly(parent_fd)


def _open_sync_home(home: Path) -> int:
    if os.fspath(home) in {"", ".", ".."}:
        raise SyncError(f"refusing unsafe sync home: {home}")
    flags = _directory_open_flags(nofollow=True)
    if home.is_absolute() and home.parent == home:
        try:
            home_fd = os.open(home, flags)
        except OSError as error:
            raise SyncError(f"refusing unsafe sync home: {home}") from error
        if not _sync_home_matches_fd(home, home_fd):
            _close_fd_quietly(home_fd)
            raise SyncError(f"sync home changed while opening it: {home}")
        return home_fd

    parent_fd = _open_sync_home_parent(home)
    try:
        try:
            home_fd = os.open(home.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise SyncError(f"refusing unsafe sync home: {home}") from error
        if not _sync_home_matches_fd(home, home_fd, parent_fd=parent_fd):
            _close_fd_quietly(home_fd)
            raise SyncError(f"sync home changed while opening it: {home}")
        return home_fd
    finally:
        _close_fd_quietly(parent_fd)


def _open_or_create_sync_home(home: Path, *, mode: int = 0o755) -> int:
    if os.fspath(home) in {"", ".", ".."}:
        raise SyncError(f"refusing unsafe sync home: {home}")
    if home.is_absolute() and home.parent == home:
        return _open_sync_home(home)
    home.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = _open_sync_home_parent(home)
    home_fd = -1
    created = False
    try:
        try:
            os.mkdir(home.name, mode=mode, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        try:
            home_fd = os.open(
                home.name,
                _directory_open_flags(nofollow=True),
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise SyncError(f"refusing unsafe sync home: {home}") from error
        if created:
            os.fsync(home_fd)
            os.fsync(parent_fd)
        if not _sync_home_matches_fd(home, home_fd, parent_fd=parent_fd):
            raise SyncError(f"sync home changed while creating it: {home}")
        result = home_fd
        home_fd = -1
        return result
    finally:
        if home_fd >= 0:
            _close_fd_quietly(home_fd)
        _close_fd_quietly(parent_fd)


def _bound_sync_home_anchor(home: Path, home_fd: int | None) -> int:
    if home_fd is None:
        return _open_sync_home(home)
    if not _sync_home_matches_fd(home, home_fd):
        raise SyncError(f"sync home changed before reopening it: {home}")
    anchor_fd = os.dup(home_fd)
    if not _sync_home_matches_fd(home, anchor_fd):
        _close_fd_quietly(anchor_fd)
        raise SyncError(f"sync home changed while reopening it: {home}")
    return anchor_fd


def _open_directory_beneath(
    home: Path,
    directory: Path,
    *,
    home_fd: int | None = None,
) -> int:
    parts = _directory_parts_beneath(home, directory)
    anchor_fd = _bound_sync_home_anchor(home, home_fd)
    directory_fd = -1
    child_flags = _directory_open_flags(nofollow=True)
    try:
        directory_fd = os.dup(anchor_fd)
        for part in parts:
            next_fd = os.open(part, child_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        if not _sync_home_matches_fd(home, anchor_fd):
            raise SyncError(f"sync home changed while opening directory: {directory}")
    except BaseException:
        if directory_fd >= 0:
            _close_fd_quietly(directory_fd)
        raise
    finally:
        _close_fd_quietly(anchor_fd)
    return directory_fd


def _open_or_create_directory_beneath(
    home: Path,
    directory: Path,
    *,
    mode: int = 0o755,
    home_fd: int | None = None,
) -> int:
    parts = _directory_parts_beneath(home, directory)
    anchor_fd = _bound_sync_home_anchor(home, home_fd)
    directory_fd = -1
    child_flags = _directory_open_flags(nofollow=True)
    try:
        directory_fd = os.dup(anchor_fd)
        for part in parts:
            try:
                next_fd = os.open(part, child_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                created = False
                try:
                    os.mkdir(part, mode=mode, dir_fd=directory_fd)
                    created = True
                except FileExistsError:
                    pass
                next_fd = os.open(part, child_flags, dir_fd=directory_fd)
                if created:
                    os.fsync(next_fd)
                    os.fsync(directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        if not _sync_home_matches_fd(home, anchor_fd):
            raise SyncError(f"sync home changed while creating directory: {directory}")
    except BaseException as error:
        if directory_fd >= 0:
            _close_fd_quietly(directory_fd)
        if isinstance(error, OSError):
            raise SyncError(f"refusing unsafe directory path: {directory}") from error
        raise
    finally:
        _close_fd_quietly(anchor_fd)
    return directory_fd


def _close_fd_quietly(file_descriptor: int) -> None:
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _directory_identity(directory_fd: int) -> tuple[int, int]:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SyncError("bound path is no longer a directory")
    return metadata.st_dev, metadata.st_ino


def _bound_directory_matches(home: Path, directory: Path, directory_fd: int) -> bool:
    expected_identity = _directory_identity(directory_fd)
    try:
        canonical_fd = _open_directory_beneath(home, directory)
    except (OSError, SyncError):
        return False
    try:
        return _directory_identity(canonical_fd) == expected_identity
    finally:
        _close_fd_quietly(canonical_fd)


def _rename_noreplace_at(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    if sys.platform == "darwin":
        rename_function = libc.renameatx_np
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_function.restype = ctypes.c_int
        result = rename_function(
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        try:
            rename_function = libc.renameat2
        except AttributeError as error:
            raise SyncError("renameat2 is required for safe sync reconciliation") from error
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_function.restype = ctypes.c_int
        result = rename_function(
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            0x00000001,
        )
    else:
        raise SyncError(
            f"safe no-replace rename is unsupported on platform {sys.platform}"
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{source_name} -> {destination_name}",
        )


def _atomic_move_beneath_home(
    home: Path,
    source: Path,
    destination: Path,
    expected_snapshot: ReconcileTargetSnapshot | None = None,
    expected_destination_parent_identity: tuple[int, int] | None = None,
) -> None:
    source_parent_fd = _open_directory_beneath(home, source.parent)
    try:
        destination_parent_fd = _open_directory_beneath(
            home,
            destination.parent,
        )
    except BaseException:
        _close_fd_quietly(source_parent_fd)
        raise
    moved = False
    moved_identity_for_restore: tuple[int, int] | None = None
    moved_target_for_restore: str | None = None
    try:
        if (
            expected_destination_parent_identity is not None
            and _directory_identity(destination_parent_fd)
            != expected_destination_parent_identity
        ):
            raise SyncError(
                f"destination parent changed after planning: {destination.parent}"
            )
        source_metadata = os.stat(
            source.name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
        source_identity = (source_metadata.st_dev, source_metadata.st_ino)
        if expected_snapshot is not None:
            if (
                expected_snapshot.parent_identity is None
                or expected_snapshot.link_identity is None
                or expected_snapshot.link_target is None
            ):
                raise SyncError(
                    f"destructive move has no planned symlink snapshot: {source}"
                )
            actual_parent_identity = _directory_identity(source_parent_fd)
            if actual_parent_identity != expected_snapshot.parent_identity:
                raise SyncError(f"source parent changed after planning: {source.parent}")
            if not stat.S_ISLNK(source_metadata.st_mode):
                raise SyncError(f"source changed after planning: {source}")
            actual_target = os.readlink(source.name, dir_fd=source_parent_fd)
            source_metadata_after = os.stat(
                source.name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
            if (
                (source_metadata_after.st_dev, source_metadata_after.st_ino)
                != source_identity
                or source_identity != expected_snapshot.link_identity
                or actual_target != expected_snapshot.link_target
            ):
                raise SyncError(f"source changed after planning: {source}")
        if not _bound_directory_matches(home, source.parent, source_parent_fd):
            raise SyncError(f"source parent changed before move: {source.parent}")
        if not _bound_directory_matches(
            home,
            destination.parent,
            destination_parent_fd,
        ):
            raise SyncError(
                f"destination parent changed before move: {destination.parent}"
            )
        _rename_noreplace_at(
            source_parent_fd,
            source.name,
            destination_parent_fd,
            destination.name,
        )
        moved = True
        os.fsync(source_parent_fd)
        if destination_parent_fd != source_parent_fd:
            os.fsync(destination_parent_fd)
        destination_metadata = os.stat(
            destination.name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
        moved_identity_for_restore = (
            destination_metadata.st_dev,
            destination_metadata.st_ino,
        )
        if stat.S_ISLNK(destination_metadata.st_mode):
            moved_identity_for_restore, moved_target_for_restore = _symlink_snapshot_at(
                destination_parent_fd,
                destination.name,
            )
        if moved_identity_for_restore != source_identity:
            raise SyncError(f"moved entry changed during reconciliation: {source}")
        if expected_snapshot is not None:
            if (
                moved_identity_for_restore != expected_snapshot.link_identity
                or moved_target_for_restore != expected_snapshot.link_target
            ):
                raise SyncError(f"source changed while moving to quarantine: {source}")
        try:
            os.stat(
                source.name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SyncError(f"source still exists after move: {source}")
        if not _bound_directory_matches(home, source.parent, source_parent_fd):
            raise SyncError(f"source parent changed during move: {source.parent}")
        if not _bound_directory_matches(
            home,
            destination.parent,
            destination_parent_fd,
        ):
            raise SyncError(
                f"destination parent changed during move: {destination.parent}"
            )
    except BaseException as error:
        if moved:
            restored = False
            try:
                _rename_noreplace_at(
                    destination_parent_fd,
                    destination.name,
                    source_parent_fd,
                    source.name,
                )
                restored = True
                os.fsync(source_parent_fd)
                if destination_parent_fd != source_parent_fd:
                    os.fsync(destination_parent_fd)
                restored_metadata = os.stat(
                    source.name,
                    dir_fd=source_parent_fd,
                    follow_symlinks=False,
                )
                if (
                    restored_metadata.st_dev,
                    restored_metadata.st_ino,
                ) != moved_identity_for_restore:
                    raise SyncError(
                        f"restored entry changed after failed move: {source}"
                    )
                if moved_target_for_restore is not None:
                    restored_identity, restored_target = _symlink_snapshot_at(
                        source_parent_fd,
                        source.name,
                    )
                    if (
                        restored_identity != moved_identity_for_restore
                        or restored_target != moved_target_for_restore
                    ):
                        raise SyncError(
                            f"restored symlink changed after failed move: {source}"
                        )
                try:
                    os.stat(
                        destination.name,
                        dir_fd=destination_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise SyncError(
                        f"failed move destination still exists: {destination}"
                    )
                if not _bound_directory_matches(
                    home,
                    source.parent,
                    source_parent_fd,
                ):
                    raise SyncError(
                        f"restored source parent changed: {source.parent}"
                    )
            except BaseException as rollback_error:
                if restored:
                    raise SyncError(
                        "safe move failed after exact source restoration could not "
                        f"be validated: {source}: {rollback_error}"
                    ) from error
                raise SyncError(
                    "safe move failed and the entry was retained at its destination: "
                    f"{destination}: {rollback_error}"
                ) from error
        raise
    finally:
        _close_fd_quietly(source_parent_fd)
        _close_fd_quietly(destination_parent_fd)


def _symlink_snapshot_at(
    parent_fd: int,
    name: str,
) -> tuple[tuple[int, int], str]:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISLNK(metadata.st_mode):
        raise SyncError(f"managed target is not a symlink: {name}")
    link_target = os.readlink(name, dir_fd=parent_fd)
    metadata_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    identity = (metadata.st_dev, metadata.st_ino)
    if (metadata_after.st_dev, metadata_after.st_ino) != identity:
        raise SyncError(f"managed symlink changed during validation: {name}")
    return identity, link_target


def _capture_reconcile_target_snapshot(
    home: Path,
    target: Path,
) -> ReconcileTargetSnapshot:
    parent_parts = _directory_parts_beneath(home, target.parent)
    child_flags = _directory_open_flags(nofollow=True)
    parent_fd = _open_directory_beneath(home, home)
    current_path = home
    try:
        for index, part in enumerate(parent_parts):
            try:
                next_fd = os.open(part, child_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                ancestor_identity = _directory_identity(parent_fd)
                if not _bound_directory_matches(home, current_path, parent_fd):
                    raise SyncError(
                        f"managed target ancestor changed: {current_path}"
                    )
                return ReconcileTargetSnapshot(
                    parent_identity=None,
                    ancestor_identity=ancestor_identity,
                    missing_parent_parts=parent_parts[index:],
                )
            _close_fd_quietly(parent_fd)
            parent_fd = next_fd
            current_path /= part
        parent_identity = _directory_identity(parent_fd)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        try:
            metadata = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not _bound_directory_matches(home, target.parent, parent_fd):
                raise SyncError(f"managed target parent changed: {target.parent}")
            return ReconcileTargetSnapshot(
                parent_identity=parent_identity,
                ancestor_identity=parent_identity,
            )
        if not stat.S_ISLNK(metadata.st_mode):
            return ReconcileTargetSnapshot(
                parent_identity=parent_identity,
                link_identity=(metadata.st_dev, metadata.st_ino),
                ancestor_identity=parent_identity,
            )
        link_identity, link_target = _symlink_snapshot_at(parent_fd, target.name)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        return ReconcileTargetSnapshot(
            parent_identity=parent_identity,
            link_identity=link_identity,
            link_target=link_target,
            ancestor_identity=parent_identity,
        )
    finally:
        _close_fd_quietly(parent_fd)


def _require_reconcile_target_snapshot(
    home: Path,
    target: Path,
    expected_snapshot: ReconcileTargetSnapshot,
) -> None:
    try:
        actual_snapshot = _capture_reconcile_target_snapshot(home, target)
    except (OSError, SyncError) as error:
        raise SyncError(f"managed target changed after planning: {target}") from error
    if actual_snapshot != expected_snapshot:
        raise SyncError(f"managed target changed after planning: {target}")


def _move_symlink_leaf_to_unique_quarantine(
    home: Path,
    source_parent_fd: int,
    source_name: str,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
    expected_link_target: str | None = None,
) -> tuple[Path, tuple[int, int], str]:
    source_identity, source_target = _symlink_snapshot_at(
        source_parent_fd,
        source_name,
    )
    if (
        (expected_identity is not None and source_identity != expected_identity)
        or (
            expected_link_target is not None
            and source_target != expected_link_target
        )
    ):
        raise SyncError(f"symlink changed before quarantine: {source_name}")
    batch_root = _quarantine_batch_root(home, [])
    quarantine_parent = batch_root / "leaf"
    quarantine_parent_fd = _open_or_create_directory_beneath(
        home,
        quarantine_parent,
        mode=0o700,
    )
    destination: Path | None = None
    try:
        if not _bound_directory_matches(
            home,
            quarantine_parent,
            quarantine_parent_fd,
        ):
            raise SyncError(f"quarantine leaf directory changed: {quarantine_parent}")
        for attempt in range(100):
            destination_name = (
                f"{label}-{os.getpid()}-{time.time_ns()}-{attempt}"
            )
            try:
                _rename_noreplace_at(
                    source_parent_fd,
                    source_name,
                    quarantine_parent_fd,
                    destination_name,
                )
            except FileExistsError:
                continue
            destination = quarantine_parent / destination_name
            break
        if destination is None:
            raise SyncError(
                f"could not allocate a unique quarantine path for {source_name}"
            )

        try:
            os.fsync(source_parent_fd)
            os.fsync(quarantine_parent_fd)
            identity, link_target = _symlink_snapshot_at(
                quarantine_parent_fd,
                destination.name,
            )
            if not _bound_directory_matches(
                home,
                quarantine_parent,
                quarantine_parent_fd,
            ):
                raise SyncError(
                    f"quarantine leaf directory changed: {quarantine_parent}"
                )
        except BaseException as error:
            raise SyncError(
                "moved symlink leaf was retained in quarantine after validation "
                f"failed: {destination}: {error}"
            ) from error
        if identity != source_identity or link_target != source_target:
            try:
                _rename_noreplace_at(
                    quarantine_parent_fd,
                    destination.name,
                    source_parent_fd,
                    source_name,
                )
                os.fsync(source_parent_fd)
                if quarantine_parent_fd != source_parent_fd:
                    os.fsync(quarantine_parent_fd)
            except BaseException as restore_error:
                raise SyncError(
                    "symlink changed during quarantine and the moved replacement "
                    f"was retained at {destination}: {restore_error}"
                ) from restore_error
            restored_identity, restored_target = _symlink_snapshot_at(
                source_parent_fd,
                source_name,
            )
            if restored_identity != identity or restored_target != link_target:
                raise SyncError(
                    "symlink changed during quarantine and its exact restoration "
                    f"could not be verified: {source_name}"
                )
            raise SyncError(
                "symlink changed during quarantine; the moved replacement was "
                f"restored without replacement: {source_name}"
            )
        return destination, identity, link_target
    finally:
        _close_fd_quietly(quarantine_parent_fd)


def _publish_reconcile_directory_noreplace(
    parent_fd: int,
    name: str,
    display_path: Path,
) -> tuple[int, tuple[int, int]]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    temporary_name: str | None = None
    directory_fd = -1
    published = False
    try:
        for attempt in range(100):
            candidate = (
                f".codex-sync-parent-{os.getpid()}-{time.time_ns()}-{attempt}"
            )
            try:
                os.mkdir(candidate, mode=0o755, dir_fd=parent_fd)
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None:
            raise SyncError(
                f"could not allocate temporary managed parent: {display_path}"
            )
        directory_fd = os.open(temporary_name, directory_flags, dir_fd=parent_fd)
        directory_identity = _directory_identity(directory_fd)
        try:
            _rename_noreplace_at(
                parent_fd,
                temporary_name,
                parent_fd,
                name,
            )
        except FileExistsError as error:
            raise SyncError(
                f"managed target parent appeared after planning: {display_path}"
            ) from error
        published = True
        temporary_name = None
        os.fsync(directory_fd)
        os.fsync(parent_fd)
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != directory_identity
        ):
            raise SyncError(
                f"managed target parent changed during creation: {display_path}"
            )
        return directory_fd, directory_identity
    except BaseException:
        if directory_fd >= 0:
            _close_fd_quietly(directory_fd)
        if temporary_name is not None:
            try:
                os.rmdir(temporary_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        if published:
            raise SyncError(
                f"created managed parent was retained after validation failed: "
                f"{display_path}"
            )
        raise


def _open_reconcile_parent_for_create(
    home: Path,
    target: Path,
    expected_snapshot: ReconcileTargetSnapshot,
    created_parent_identities: dict[Path, tuple[int, int]],
) -> int:
    if expected_snapshot.link_identity is not None:
        raise SyncError(f"create target was not absent when planned: {target}")
    if expected_snapshot.parent_identity is not None:
        _require_reconcile_target_snapshot(home, target, expected_snapshot)
        parent_fd = _open_directory_beneath(home, target.parent)
        if _directory_identity(parent_fd) != expected_snapshot.parent_identity:
            _close_fd_quietly(parent_fd)
            raise SyncError(
                f"managed target parent changed after planning: {target.parent}"
            )
        return parent_fd

    parent_parts = _directory_parts_beneath(home, target.parent)
    missing_parts = expected_snapshot.missing_parent_parts
    if (
        expected_snapshot.ancestor_identity is None
        or len(missing_parts) > len(parent_parts)
        or parent_parts[-len(missing_parts) :] != missing_parts
    ):
        raise SyncError(f"invalid missing-parent planning snapshot: {target.parent}")
    ancestor_parts = parent_parts[: len(parent_parts) - len(missing_parts)]
    ancestor_path = home.joinpath(*ancestor_parts)
    parent_fd = _open_directory_beneath(home, ancestor_path)
    try:
        if (
            _directory_identity(parent_fd) != expected_snapshot.ancestor_identity
            or not _bound_directory_matches(home, ancestor_path, parent_fd)
        ):
            raise SyncError(
                f"managed target ancestor changed after planning: {ancestor_path}"
            )
        current_path = ancestor_path
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        for part in missing_parts:
            next_path = current_path / part
            expected_identity = created_parent_identities.get(next_path)
            if expected_identity is None:
                next_fd, expected_identity = _publish_reconcile_directory_noreplace(
                    parent_fd,
                    part,
                    next_path,
                )
                created_parent_identities[next_path] = expected_identity
            else:
                try:
                    next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                except OSError as error:
                    raise SyncError(
                        f"transaction-created managed parent changed: {next_path}"
                    ) from error
                if _directory_identity(next_fd) != expected_identity:
                    _close_fd_quietly(next_fd)
                    raise SyncError(
                        f"transaction-created managed parent changed: {next_path}"
                    )
            _close_fd_quietly(parent_fd)
            parent_fd = next_fd
            current_path = next_path
            if not _bound_directory_matches(home, current_path, parent_fd):
                raise SyncError(
                    f"transaction-created managed parent changed: {current_path}"
                )
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return parent_fd
        raise SyncError(f"managed target appeared after planning: {target}")
    except BaseException:
        _close_fd_quietly(parent_fd)
        raise


def _create_symlink_beneath(
    home: Path,
    target: Path,
    link_target: str,
    kind: str,
    expected_snapshot: ReconcileTargetSnapshot | None = None,
    created_parent_identities: dict[Path, tuple[int, int]] | None = None,
) -> SymlinkSnapshot:
    _ensure_safe_target_parent(home, target)
    if expected_snapshot is not None:
        if created_parent_identities is None:
            created_parent_identities = {}
        parent_fd = _open_reconcile_parent_for_create(
            home,
            target,
            expected_snapshot,
            created_parent_identities,
        )
    else:
        parent_fd = _open_or_create_directory_beneath(home, target.parent)
    created = False
    created_identity: tuple[int, int] | None = None
    try:
        parent_identity = _directory_identity(parent_fd)
        if (
            expected_snapshot is not None
            and expected_snapshot.parent_identity is not None
            and parent_identity != expected_snapshot.parent_identity
        ):
            raise SyncError(f"managed target parent changed after planning: {target.parent}")
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        os.symlink(
            link_target,
            target.name,
            target_is_directory=kind in {"directory", "skill"},
            dir_fd=parent_fd,
        )
        created = True
        created_identity, actual_target = _symlink_snapshot_at(parent_fd, target.name)
        if actual_target != link_target:
            raise SyncError(f"created managed symlink changed: {target}")
        os.fsync(parent_fd)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        return SymlinkSnapshot(
            parent_identity=parent_identity,
            link_identity=created_identity,
            link_target=actual_target,
        )
    except BaseException as error:
        if not created:
            raise
        if created_identity is None:
            raise SyncError(
                "managed symlink creation failed before its inode was bound; "
                f"the target was left in place: {target}"
            ) from error
        try:
            quarantine_path, moved_identity, moved_target = (
                _move_symlink_leaf_to_unique_quarantine(
                    home,
                    parent_fd,
                    target.name,
                    label="create-cleanup",
                    expected_identity=created_identity,
                    expected_link_target=link_target,
                )
            )
        except BaseException as cleanup_error:
            raise SyncError(
                "managed symlink creation failed and cleanup could not safely "
                f"quarantine {target}: {cleanup_error}"
            ) from error
        if (
            created_identity is None
            or moved_identity != created_identity
            or moved_target != link_target
        ):
            raise SyncError(
                "created managed symlink changed before cleanup and was retained "
                f"in quarantine: {target} -> {quarantine_path}"
            ) from error
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SyncError(
                "managed symlink cleanup found a replacement leaf and left it in "
                f"place: {target}; created symlink retained in quarantine: "
                f"{quarantine_path}"
            ) from error
        raise
    finally:
        _close_fd_quietly(parent_fd)


def _publish_symlink_hardlink_beneath(
    home: Path,
    source: Path,
    target: Path,
    source_snapshot: SymlinkSnapshot,
    expected_target_snapshot: ReconcileTargetSnapshot,
    created_parent_identities: dict[Path, tuple[int, int]],
) -> SymlinkSnapshot:
    source_parent_fd = _open_directory_beneath(home, source.parent)
    try:
        target_parent_fd = _open_reconcile_parent_for_create(
            home,
            target,
            expected_target_snapshot,
            created_parent_identities,
        )
    except BaseException:
        _close_fd_quietly(source_parent_fd)
        raise
    created = False
    created_identity: tuple[int, int] | None = None
    try:
        source_identity, source_target = _symlink_snapshot_at(
            source_parent_fd,
            source.name,
        )
        if (
            _directory_identity(source_parent_fd)
            != source_snapshot.parent_identity
            or source_identity != source_snapshot.link_identity
            or source_target != source_snapshot.link_target
            or not _bound_directory_matches(home, source.parent, source_parent_fd)
        ):
            raise SyncError(f"pending link stage changed: {source}")
        if not _bound_directory_matches(home, target.parent, target_parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        try:
            os.link(
                source.name,
                target.name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise SyncError(
                f"failed to publish inode-bound managed symlink {target}: {error}"
            ) from error
        created = True
        created_identity, created_target = _symlink_snapshot_at(
            target_parent_fd,
            target.name,
        )
        if (
            created_identity != source_snapshot.link_identity
            or created_target != source_snapshot.link_target
        ):
            raise SyncError(f"published managed symlink changed: {target}")
        os.fsync(target_parent_fd)
        if (
            not _bound_directory_matches(home, source.parent, source_parent_fd)
            or not _bound_directory_matches(home, target.parent, target_parent_fd)
        ):
            raise SyncError(f"managed link parent changed during publication: {target}")
        source_identity_after, source_target_after = _symlink_snapshot_at(
            source_parent_fd,
            source.name,
        )
        target_identity_after, target_value_after = _symlink_snapshot_at(
            target_parent_fd,
            target.name,
        )
        if (
            source_identity_after != source_snapshot.link_identity
            or source_target_after != source_snapshot.link_target
            or target_identity_after != source_snapshot.link_identity
            or target_value_after != source_snapshot.link_target
        ):
            raise SyncError(f"published managed symlink changed: {target}")
        return SymlinkSnapshot(
            parent_identity=_directory_identity(target_parent_fd),
            link_identity=target_identity_after,
            link_target=target_value_after,
        )
    except BaseException as error:
        if not created or created_identity is None:
            raise
        try:
            _remove_expected_symlink_beneath(
                home,
                target,
                source_snapshot.link_target,
                SymlinkSnapshot(
                    parent_identity=_directory_identity(target_parent_fd),
                    link_identity=created_identity,
                    link_target=source_snapshot.link_target,
                ),
            )
        except BaseException as cleanup_error:
            raise SyncError(
                "managed symlink publication failed and exact cleanup was "
                f"incomplete: {target}: {cleanup_error}"
            ) from error
        raise
    finally:
        _close_fd_quietly(target_parent_fd)
        _close_fd_quietly(source_parent_fd)


def _read_symlink_snapshot_beneath(
    home: Path,
    target: Path,
) -> SymlinkSnapshot:
    parent_fd = _open_directory_beneath(home, target.parent)
    try:
        parent_identity = _directory_identity(parent_fd)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        link_identity, link_target = _symlink_snapshot_at(parent_fd, target.name)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        return SymlinkSnapshot(
            parent_identity=parent_identity,
            link_identity=link_identity,
            link_target=link_target,
        )
    finally:
        _close_fd_quietly(parent_fd)


def _read_optional_symlink_snapshot_beneath(
    home: Path,
    target: Path,
) -> SymlinkSnapshot | None:
    try:
        parent_fd = _open_directory_beneath(home, target.parent)
    except FileNotFoundError:
        return None
    try:
        parent_identity = _directory_identity(parent_fd)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        try:
            metadata = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not _bound_directory_matches(home, target.parent, parent_fd):
                raise SyncError(f"managed target parent changed: {target.parent}")
            return None
        if not stat.S_ISLNK(metadata.st_mode):
            if not _bound_directory_matches(home, target.parent, parent_fd):
                raise SyncError(f"managed target parent changed: {target.parent}")
            return None
        link_identity, link_target = _symlink_snapshot_at(parent_fd, target.name)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        return SymlinkSnapshot(
            parent_identity=parent_identity,
            link_identity=link_identity,
            link_target=link_target,
        )
    finally:
        _close_fd_quietly(parent_fd)


def _read_symlink_beneath(home: Path, target: Path) -> str:
    return _read_symlink_snapshot_beneath(home, target).link_target


def _remove_expected_symlink_beneath(
    home: Path,
    target: Path,
    expected_link_target: str,
    expected_snapshot: SymlinkSnapshot | None = None,
) -> None:
    parent_fd = _open_directory_beneath(home, target.parent)
    try:
        parent_identity = _directory_identity(parent_fd)
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(f"managed target parent changed: {target.parent}")
        identity, actual_target = _symlink_snapshot_at(parent_fd, target.name)
        actual_snapshot = SymlinkSnapshot(
            parent_identity=parent_identity,
            link_identity=identity,
            link_target=actual_target,
        )
        if (
            actual_target != expected_link_target
            or (
                expected_snapshot is not None
                and actual_snapshot != expected_snapshot
            )
        ):
            raise SyncError(f"refusing to remove changed managed symlink: {target}")
        quarantine_path, moved_identity, moved_target = (
            _move_symlink_leaf_to_unique_quarantine(
                home,
                parent_fd,
                target.name,
                label="remove",
                expected_identity=identity,
                expected_link_target=expected_link_target,
            )
        )
        if (
            moved_identity != identity
            or moved_target != expected_link_target
            or (
                expected_snapshot is not None
                and (
                    moved_identity != expected_snapshot.link_identity
                    or moved_target != expected_snapshot.link_target
                )
            )
        ):
            raise SyncError(
                "managed symlink changed during removal and was retained in "
                f"quarantine: {target} -> {quarantine_path}"
            )
        if not _bound_directory_matches(home, target.parent, parent_fd):
            raise SyncError(
                "managed target parent changed during removal; symlink was retained "
                f"in quarantine: {quarantine_path}"
            )
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise SyncError(
            "managed target reappeared after removal and was left in place: "
            f"{target}; removed symlink retained in quarantine: {quarantine_path}"
        )
    finally:
        _close_fd_quietly(parent_fd)


def _ensure_safe_release_directory(
    home: Path,
    owner: str,
    sha: str,
    *,
    allow_missing: bool,
) -> bool:
    sha = _validate_release_sha(sha, f"release SHA for owner {owner}")
    releases_root = _releases_root(home, owner)
    if not _ensure_safe_internal_directory(
        home,
        releases_root,
        create=False,
        allow_missing=allow_missing,
    ):
        return False
    release_dir = releases_root / sha
    try:
        mode = os.lstat(release_dir).st_mode
    except FileNotFoundError:
        if allow_missing:
            return False
        raise SyncError(f"release directory is missing: {release_dir}")
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise SyncError(f"refusing unsafe release directory: {release_dir}")
    return True


def _removed_link_key(removed: RemovedLink) -> str:
    return f"{removed.owner}:{removed.id}"


def _empty_managed_state() -> ManagedState:
    return ManagedState(owners={}, links={})


def _managed_state_metadata_snapshot(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_managed_state_bytes(file_fd: int, path: Path) -> bytes:
    payload = bytearray()
    try:
        while len(payload) <= MAX_MANAGED_STATE_BYTES:
            chunk = os.read(
                file_fd,
                min(1024 * 1024, MAX_MANAGED_STATE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
    except OSError as error:
        raise SyncError(f"Failed to read {path}: {error}") from error
    if len(payload) > MAX_MANAGED_STATE_BYTES:
        raise SyncError(
            f"Failed to read {path}: managed link state exceeds "
            f"{MAX_MANAGED_STATE_BYTES} bytes"
        )
    return bytes(payload)


def _decode_managed_state_json(payload: bytes, path: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            payload.decode("utf-8"),
            parse_int=_bounded_json_integer,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SyncError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise SyncError(f"Expected JSON object in {path}")
    return data


def _read_managed_state_file_snapshot(
    home: Path,
    path: Path,
    parent_fd: int,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> ManagedStateFileSnapshot:
    if not _bound_directory_matches(home, path.parent, parent_fd):
        raise SyncError(f"managed sync state parent changed before read: {path}")
    parent_identity = _directory_identity(parent_fd)
    try:
        named_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if not _bound_directory_matches(home, path.parent, parent_fd):
            raise SyncError(
                f"managed sync state parent changed before read: {path}"
            )
        return ManagedStateFileSnapshot(
            exists=False,
            parent_identity=parent_identity,
        )
    except OSError as error:
        raise SyncError(f"Failed to read {path}: {error}") from error
    if not stat.S_ISREG(named_metadata.st_mode):
        raise SyncError(f"refusing non-file sync state: {path}")
    expected_snapshot = _managed_state_metadata_snapshot(named_metadata)
    if (
        expected_identity is not None
        and (named_metadata.st_dev, named_metadata.st_ino) != expected_identity
    ):
        raise SyncError(f"managed sync state changed before read: {path}")
    if named_metadata.st_size > MAX_MANAGED_STATE_BYTES:
        raise SyncError(
            f"Failed to read {path}: managed link state exceeds "
            f"{MAX_MANAGED_STATE_BYTES} bytes"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    file_fd = -1
    try:
        try:
            file_fd = os.open(path.name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise SyncError(f"Failed to read {path}: {error}") from error
        opened_metadata = os.fstat(file_fd)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise SyncError(f"refusing non-file sync state: {path}")
        if _managed_state_metadata_snapshot(opened_metadata) != expected_snapshot:
            raise SyncError(f"managed sync state changed before read: {path}")
        if (
            expected_identity is not None
            and (opened_metadata.st_dev, opened_metadata.st_ino)
            != expected_identity
        ):
            raise SyncError(f"managed sync state changed before read: {path}")
        if not _bound_directory_matches(home, path.parent, parent_fd):
            raise SyncError(
                f"managed sync state parent changed before read: {path}"
            )

        payload = _read_managed_state_bytes(file_fd, path)
        if (
            _managed_state_metadata_snapshot(os.fstat(file_fd))
            != expected_snapshot
        ):
            raise SyncError(f"managed sync state changed during read: {path}")
        try:
            current_metadata = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise SyncError(
                f"managed sync state changed during read: {path}"
            ) from error
        if _managed_state_metadata_snapshot(current_metadata) != expected_snapshot:
            raise SyncError(f"managed sync state changed during read: {path}")
        if not _bound_directory_matches(home, path.parent, parent_fd):
            raise SyncError(
                f"managed sync state parent changed during read: {path}"
            )
    finally:
        if file_fd >= 0:
            _close_fd_quietly(file_fd)

    return ManagedStateFileSnapshot(
        exists=True,
        payload=payload,
        mode=stat.S_IMODE(opened_metadata.st_mode),
        parent_identity=parent_identity,
        file_identity=(opened_metadata.st_dev, opened_metadata.st_ino),
    )


def _load_managed_state_payload_with_snapshot(
    home: Path,
    path: Path,
) -> tuple[dict[str, Any] | None, ManagedStateFileSnapshot]:
    if not _ensure_safe_internal_parent(
        home,
        path,
        create=False,
        allow_missing=True,
    ):
        return None, ManagedStateFileSnapshot(exists=False)

    try:
        parent_fd = _open_directory_beneath(home, path.parent)
    except (OSError, SyncError) as error:
        raise SyncError(f"Failed to read {path}: {error}") from error
    try:
        snapshot = _read_managed_state_file_snapshot(
            home,
            path,
            parent_fd,
        )
    finally:
        _close_fd_quietly(parent_fd)

    if not snapshot.exists:
        return None, snapshot
    assert snapshot.payload is not None
    return _decode_managed_state_json(snapshot.payload, path), snapshot


def _load_managed_state_payload(home: Path, path: Path) -> dict[str, Any] | None:
    data, _snapshot = _load_managed_state_payload_with_snapshot(home, path)
    return data


def _managed_state_from_payload(
    home: Path,
    data: dict[str, Any],
    manifest_entry_indexes: (
        dict[
            tuple[str, str],
            dict[tuple[PurePosixPath, PurePosixPath, str, str], LinkEntry],
        ]
        | None
    ) = None,
) -> ManagedState:
    version = data.get("version")
    if type(version) is not int or version != 1:
        raise SyncError("managed link state version must be 1")

    raw_owners = data.get("owners", {})
    if not isinstance(raw_owners, dict):
        raise SyncError("managed link state owners must be an object")
    owners: dict[str, str] = {}
    for raw_owner, raw_sha in raw_owners.items():
        owner = _validate_owner(raw_owner, "managed link owner")
        if not isinstance(raw_sha, str) or RELEASE_DIR_RE.fullmatch(raw_sha) is None:
            raise SyncError(f"managed link owner {owner} has invalid release SHA")
        owners[owner] = raw_sha

    raw_links = data.get("links", [])
    if not isinstance(raw_links, list):
        raise SyncError("managed link state links must be an array")
    links: dict[PurePosixPath, ManagedLinkRecord] = {}
    for index, raw_link in enumerate(raw_links):
        if not isinstance(raw_link, dict):
            raise SyncError(f"managed link state entry #{index + 1} must be an object")
        source = _validate_relative_path(raw_link.get("source"), "managed link source")
        target = _validate_relative_path(raw_link.get("target"), "managed link target")
        kind = raw_link.get("kind")
        if kind not in {"file", "directory", "skill"}:
            raise SyncError(f"managed link {target} has unsupported kind: {kind}")
        owner = _validate_owner(raw_link.get("owner"), "managed link owner")
        link_target = raw_link.get("link_target")
        if not isinstance(link_target, str) or not link_target:
            raise SyncError(f"managed link {target} has invalid link_target")
        release_sha = raw_link.get("release_sha")
        if not isinstance(release_sha, str) or RELEASE_DIR_RE.fullmatch(release_sha) is None:
            raise SyncError(f"managed link {target} has invalid release SHA")
        if target in links:
            raise SyncError(f"duplicate managed link target: {target}")
        links[target] = ManagedLinkRecord(
            source=source,
            target=target,
            kind=kind,
            owner=owner,
            link_target=link_target,
            release_sha=release_sha,
        )

    if manifest_entry_indexes is None:
        manifest_entry_indexes = {}
    for owner, sha in owners.items():
        _ensure_safe_release_directory(
            home,
            owner,
            sha,
            allow_missing=False,
        )
        try:
            manifest = _load_installed_manifest_data(home, owner, sha)
        except SyncError as error:
            raise SyncError(
                f"managed link state references invalid release {owner}@{sha}: {error}"
            ) from error
        if manifest.owner != owner:
            raise SyncError(
                f"managed link state release owner mismatch: expected {owner}, "
                f"got {manifest.owner}"
            )
        manifest_entry_indexes[(owner, sha)] = {
            (entry.source, entry.target, entry.kind, entry.owner): entry
            for entry in manifest.entries
        }

    for record in links.values():
        owner_sha = owners.get(record.owner)
        if owner_sha != record.release_sha:
            raise SyncError(
                f"managed link {record.target} owner/release does not match state owners"
            )
        matching_entry = manifest_entry_indexes[
            (record.owner, record.release_sha)
        ].get(
            (
                record.source,
                record.target,
                record.kind,
                record.owner,
            )
        )
        if matching_entry is None:
            raise SyncError(
                f"managed link {record.target} is not declared by "
                f"{record.owner}@{record.release_sha}"
            )
        expected_link_target = _desired_link_target(home, matching_entry)
        if record.link_target != expected_link_target:
            raise SyncError(
                f"managed link {record.target} has unexpected link_target"
            )

    return ManagedState(owners=owners, links=links)


def _load_managed_state_with_snapshot(
    home: Path,
) -> tuple[ManagedState, ManagedStateFileSnapshot]:
    path = _state_path(home)
    data, snapshot = _load_managed_state_payload_with_snapshot(home, path)
    if data is None:
        return _empty_managed_state(), snapshot
    return _managed_state_from_payload(home, data), snapshot


def _load_managed_state(home: Path) -> ManagedState:
    state, _snapshot = _load_managed_state_with_snapshot(home)
    return state


def _managed_state_payload(state: ManagedState) -> dict[str, Any]:
    return {
        "version": 1,
        "owners": dict(sorted(state.owners.items())),
        "links": [
            {
                "source": record.source.as_posix(),
                "target": record.target.as_posix(),
                "kind": record.kind,
                "owner": record.owner,
                "link_target": record.link_target,
                "release_sha": record.release_sha,
            }
            for record in sorted(state.links.values(), key=lambda item: item.target.as_posix())
        ],
    }


def _managed_state_bytes(state: ManagedState) -> bytes:
    payload = json.dumps(_managed_state_payload(state), indent=2, sort_keys=False) + "\n"
    return payload.encode("utf-8")


def _snapshot_managed_state_file(home: Path) -> ManagedStateFileSnapshot:
    path = _state_path(home)
    if not _ensure_safe_internal_parent(
        home,
        path,
        create=False,
        allow_missing=True,
    ):
        return ManagedStateFileSnapshot(exists=False)
    try:
        parent_fd = _open_directory_beneath(home, path.parent)
    except (OSError, SyncError) as error:
        raise SyncError(f"refusing unsafe sync state: {path}") from error
    try:
        return _read_managed_state_file_snapshot(home, path, parent_fd)
    finally:
        _close_fd_quietly(parent_fd)


def _managed_state_file_matches(
    home: Path,
    path: Path,
    snapshot: ManagedStateFileSnapshot,
    expected_identity: tuple[int, int] | None = None,
    parent_fd: int | None = None,
) -> bool:
    effective_identity = expected_identity
    if effective_identity is None:
        effective_identity = snapshot.file_identity
    owns_parent_fd = parent_fd is None
    if parent_fd is None:
        try:
            parent_fd = _open_directory_beneath(home, path.parent)
        except (OSError, SyncError):
            return False
    assert parent_fd is not None
    try:
        current = _read_managed_state_file_snapshot(
            home,
            path,
            parent_fd,
            expected_identity=effective_identity,
        )
    except (OSError, SyncError):
        return False
    finally:
        if owns_parent_fd:
            _close_fd_quietly(parent_fd)
    if current.exists != snapshot.exists:
        return False
    if not snapshot.exists:
        return True
    assert current.payload is not None
    assert current.mode is not None
    return (
        current.mode == snapshot.mode
        and current.payload == snapshot.payload
        and (
            effective_identity is None
            or current.file_identity == effective_identity
        )
    )


def _canonical_managed_state_matches_snapshot(
    home: Path,
    snapshot: ManagedStateFileSnapshot,
) -> bool:
    path = _state_path(home)
    if not _ensure_safe_internal_parent(
        home,
        path,
        create=False,
        allow_missing=True,
    ):
        return not snapshot.exists and snapshot.parent_identity is None
    try:
        with _managed_state_directory_fd(home, path.parent) as parent_fd:
            if (
                snapshot.parent_identity is not None
                and _directory_identity(parent_fd) != snapshot.parent_identity
            ):
                return False
            return _managed_state_file_matches(
                home,
                path,
                snapshot,
                snapshot.file_identity,
                parent_fd=parent_fd,
            )
    except (OSError, SyncError):
        return False


def _prepare_managed_state_transaction(
    home: Path,
    state: ManagedState,
    before_snapshot: ManagedStateFileSnapshot | None = None,
) -> ManagedStateFileTransaction:
    if before_snapshot is None:
        before_snapshot = _snapshot_managed_state_file(home)
    if before_snapshot.exists and (
        before_snapshot.parent_identity is None
        or before_snapshot.file_identity is None
    ):
        raise SyncError("managed state planning snapshot lacks canonical identity")
    if not _canonical_managed_state_matches_snapshot(home, before_snapshot):
        raise SyncError(
            f"sync state changed after planning: {_state_path(home)}"
        )
    return ManagedStateFileTransaction(
        before=before_snapshot,
        after=ManagedStateFileSnapshot(
            exists=True,
            payload=_managed_state_bytes(state),
            mode=0o600,
        ),
    )


def _fsync_directory(path: Path, directory_fd: int | None = None) -> None:
    if directory_fd is not None:
        os.fsync(directory_fd)
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextlib.contextmanager
def _managed_state_directory_fd(home: Path, directory: Path):
    directory_fd = _open_directory_beneath(home, directory)
    try:
        yield directory_fd
    finally:
        _close_fd_quietly(directory_fd)


def _managed_state_identity(file_fd: int) -> tuple[int, int]:
    metadata = os.fstat(file_fd)
    return metadata.st_dev, metadata.st_ino


def _write_managed_state_temp(
    parent_fd: int,
    path: Path,
    snapshot: ManagedStateFileSnapshot,
    label: str,
) -> tuple[str, tuple[int, int]]:
    assert snapshot.exists
    assert snapshot.payload is not None
    assert snapshot.mode is not None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    file_fd = -1
    temp_name = ""
    for attempt in range(100):
        temp_name = (
            f".{path.name}.{label}.{os.getpid()}.{time.time_ns()}.{attempt}.tmp"
        )
        try:
            file_fd = os.open(
                temp_name,
                flags,
                snapshot.mode,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        break
    else:
        raise SyncError(f"could not allocate managed state temporary file for {path}")

    try:
        identity = _managed_state_identity(file_fd)
        file_object = os.fdopen(file_fd, "wb", closefd=True)
        file_fd = -1
        with file_object as file:
            os.fchmod(file.fileno(), snapshot.mode)
            file.write(snapshot.payload)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        if file_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(file_fd)
        raise
    return temp_name, identity


@contextlib.contextmanager
def _managed_state_quarantine_directory_fd(
    home: Path,
    transaction: ManagedStateFileTransaction,
):
    if transaction.batch_root is None:
        transaction.batch_root = _quarantine_batch_root(home, [])
    assert transaction.batch_root is not None
    state_fd = _open_or_create_directory_beneath(
        home,
        transaction.batch_root / "state",
        mode=0o700,
    )
    try:
        yield state_fd
    finally:
        _close_fd_quietly(state_fd)


def _move_managed_state_entry_to_quarantine(
    home: Path,
    transaction: ManagedStateFileTransaction,
    source_parent_fd: int,
    source_name: str,
    label: str,
    *,
    destination_name: str | None = None,
    expected_identity: tuple[int, int] | None = None,
    expected_snapshot: ManagedStateFileSnapshot | None = None,
) -> tuple[Path, bool]:
    source_metadata = os.stat(
        source_name,
        dir_fd=source_parent_fd,
        follow_symlinks=False,
    )
    source_identity = (source_metadata.st_dev, source_metadata.st_ino)
    if expected_identity is not None and source_identity != expected_identity:
        raise SyncError(
            f"managed state entry changed before quarantine: {source_name}"
        )

    with _managed_state_quarantine_directory_fd(home, transaction) as quarantine_fd:
        selected_name = destination_name
        for attempt in range(100):
            if selected_name is None:
                selected_name = f"{label}-{time.time_ns()}-{attempt}"
            try:
                _rename_noreplace_at(
                    source_parent_fd,
                    source_name,
                    quarantine_fd,
                    selected_name,
                )
            except FileExistsError:
                if destination_name is not None:
                    raise
                selected_name = None
                continue
            break
        else:
            raise SyncError(
                f"could not allocate quarantine path for managed state {source_name}"
            )

        os.fsync(quarantine_fd)
        os.fsync(source_parent_fd)

        moved_metadata = os.stat(
            selected_name,
            dir_fd=quarantine_fd,
            follow_symlinks=False,
        )
        moved_identity = (moved_metadata.st_dev, moved_metadata.st_ino)
        matches = moved_identity == source_identity
        if expected_identity is not None:
            matches = matches and moved_identity == expected_identity
        if expected_snapshot is not None:
            assert transaction.batch_root is not None
            matches = matches and _managed_state_file_matches(
                home,
                transaction.batch_root / "state" / selected_name,
                expected_snapshot,
                moved_identity,
                parent_fd=quarantine_fd,
            )
        if not matches:
            assert selected_name is not None
            assert transaction.batch_root is not None
            quarantine_path = transaction.batch_root / "state" / selected_name
            try:
                _rename_noreplace_at(
                    quarantine_fd,
                    selected_name,
                    source_parent_fd,
                    source_name,
                )
            except BaseException as restore_error:
                raise SyncError(
                    "managed state entry changed during quarantine and the moved "
                    f"replacement was retained at {quarantine_path}: {restore_error}"
                ) from restore_error
            os.fsync(source_parent_fd)
            os.fsync(quarantine_fd)
            restored_metadata = os.stat(
                source_name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
            if (restored_metadata.st_dev, restored_metadata.st_ino) != moved_identity:
                raise SyncError(
                    "managed state entry changed during quarantine and its exact "
                    f"restoration could not be verified: {source_name}"
                )
            if _managed_state_name_exists(quarantine_fd, selected_name):
                raise SyncError(
                    "managed state entry changed during quarantine and the "
                    f"destination still exists: {quarantine_path}"
                )
            raise SyncError(
                "managed state entry changed during quarantine; the moved "
                f"replacement was restored without replacement: {source_name}"
            )

    assert transaction.batch_root is not None
    return transaction.batch_root / "state" / selected_name, matches


def _managed_state_quarantine_file_matches(
    home: Path,
    transaction: ManagedStateFileTransaction,
    path: Path,
    snapshot: ManagedStateFileSnapshot,
) -> bool:
    if transaction.batch_root is None:
        return False
    expected_parent = transaction.batch_root / "state"
    if path.parent != expected_parent:
        return False
    try:
        with _managed_state_quarantine_directory_fd(home, transaction) as parent_fd:
            return _managed_state_file_matches(
                home,
                path,
                snapshot,
                parent_fd=parent_fd,
            )
    except (OSError, SyncError):
        return False


def _canonical_managed_state_matches(
    home: Path,
    path: Path,
    snapshot: ManagedStateFileSnapshot,
    parent_identity: tuple[int, int],
    file_identity: tuple[int, int],
) -> bool:
    try:
        with _managed_state_directory_fd(home, path.parent) as canonical_parent_fd:
            if _directory_identity(canonical_parent_fd) != parent_identity:
                return False
            return _managed_state_file_matches(
                home,
                path,
                snapshot,
                file_identity,
                parent_fd=canonical_parent_fd,
            )
    except (OSError, SyncError):
        return False


def _managed_state_name_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _apply_managed_state_transaction(
    home: Path,
    transaction: ManagedStateFileTransaction,
) -> None:
    path = _state_path(home)
    state_dir = path.parent
    _ensure_safe_internal_parent(home, path, create=True)
    with _managed_state_directory_fd(home, state_dir) as state_dir_fd:
        parent_identity = _directory_identity(state_dir_fd)
        transaction.state_parent_identity = parent_identity
        try:
            if (
                transaction.before.parent_identity is not None
                and parent_identity != transaction.before.parent_identity
            ):
                raise SyncError(
                    f"sync state parent changed after planning: {state_dir}"
                )
            if transaction.before.exists:
                if not _managed_state_file_matches(
                    home,
                    path,
                    transaction.before,
                    transaction.before.file_identity,
                    parent_fd=state_dir_fd,
                ):
                    raise SyncError(f"sync state changed before write: {path}")
                if transaction.before_evidence is not None:
                    evidence_parent_fd = _open_directory_beneath(
                        home,
                        transaction.before_evidence.parent,
                    )
                    try:
                        evidence_snapshot = _read_managed_state_file_snapshot(
                            home,
                            transaction.before_evidence,
                            evidence_parent_fd,
                            expected_identity=transaction.before.file_identity,
                        )
                    finally:
                        _close_fd_quietly(evidence_parent_fd)
                    if (
                        evidence_snapshot.file_identity
                        != transaction.before.file_identity
                        or evidence_snapshot.payload != transaction.before.payload
                        or evidence_snapshot.mode != transaction.before.mode
                    ):
                        raise SyncError("managed state before evidence changed")
                backup, backup_matches = _move_managed_state_entry_to_quarantine(
                    home,
                    transaction,
                    state_dir_fd,
                    path.name,
                    "original",
                    destination_name=path.name,
                    expected_identity=transaction.before.file_identity,
                    expected_snapshot=transaction.before,
                )
                transaction.backup = backup
                if not backup_matches:
                    raise SyncError(
                        "sync state changed before write and was retained in quarantine: "
                        f"{path} -> {backup}"
                    )
            elif not _managed_state_file_matches(
                home,
                path,
                transaction.before,
                parent_fd=state_dir_fd,
            ):
                raise SyncError(f"unexpected sync state appeared before write: {path}")

            if transaction.after_evidence is None:
                temp_name, publication_identity = _write_managed_state_temp(
                    state_dir_fd,
                    path,
                    transaction.after,
                    "publish",
                )
                _rename_noreplace_at(
                    state_dir_fd,
                    temp_name,
                    state_dir_fd,
                    path.name,
                )
            else:
                if transaction.after_evidence_identity is None:
                    raise SyncError("managed state after evidence has no identity")
                evidence_parent_fd = _open_directory_beneath(
                    home,
                    transaction.after_evidence.parent,
                )
                try:
                    evidence_snapshot = _read_managed_state_file_snapshot(
                        home,
                        transaction.after_evidence,
                        evidence_parent_fd,
                        expected_identity=transaction.after_evidence_identity,
                    )
                    if (
                        evidence_snapshot.payload != transaction.after.payload
                        or evidence_snapshot.mode != transaction.after.mode
                    ):
                        raise SyncError("managed state after evidence changed")
                    os.link(
                        transaction.after_evidence.name,
                        path.name,
                        src_dir_fd=evidence_parent_fd,
                        dst_dir_fd=state_dir_fd,
                        follow_symlinks=False,
                    )
                finally:
                    _close_fd_quietly(evidence_parent_fd)
                publication_identity = transaction.after_evidence_identity
            transaction.published = True
            transaction.published_identity = publication_identity
            if not _managed_state_file_matches(
                home,
                path,
                transaction.after,
                publication_identity,
                parent_fd=state_dir_fd,
            ):
                raise SyncError(f"published sync state changed before verification: {path}")
            _fsync_directory(state_dir, state_dir_fd)
            if not _canonical_managed_state_matches(
                home,
                path,
                transaction.after,
                parent_identity,
                publication_identity,
            ):
                raise SyncError(
                    f"published sync state changed after fsync or its parent changed: {path}"
                )
        except BaseException as error:
            retained_path: Path | None = None
            cleanup_error: BaseException | None = None
            if transaction.published and transaction.published_identity is not None:
                try:
                    retained_path, retained_matches = (
                        _move_managed_state_entry_to_quarantine(
                            home,
                            transaction,
                            state_dir_fd,
                            path.name,
                            "publish-error",
                            expected_identity=transaction.published_identity,
                        )
                    )
                    transaction.published = False
                    transaction.published_identity = None
                    if not retained_matches:
                        cleanup_error = SyncError(
                            "published sync state changed while being quarantined"
                        )
                except (OSError, SyncError) as quarantine_error:
                    cleanup_error = quarantine_error
            if cleanup_error is not None:
                raise SyncError(
                    "managed state publication failed and its bound published path "
                    f"could not be safely quarantined: {cleanup_error}"
                ) from error
            if retained_path is not None:
                raise SyncError(
                    f"managed state publication failed: {error}; "
                    f"retained in quarantine: {retained_path}"
                ) from error
            raise

def _restore_managed_state_file(
    home: Path,
    transaction: ManagedStateFileTransaction | None,
) -> None:
    if transaction is None:
        return
    path = _state_path(home)
    if not _ensure_safe_internal_parent(
        home,
        path,
        create=False,
        allow_missing=True,
    ):
        raise SyncError(f"sync state parent disappeared during rollback: {path.parent}")

    with _managed_state_directory_fd(home, path.parent) as state_dir_fd:
        parent_identity = _directory_identity(state_dir_fd)
        if (
            transaction.state_parent_identity is not None
            and parent_identity != transaction.state_parent_identity
        ):
            raise SyncError(
                f"sync state parent changed during rollback and was left in place: {path.parent}"
            )

        if transaction.published:
            if transaction.published_identity is None:
                raise SyncError(
                    f"published sync state identity is missing during rollback: {path}"
                )
            if not _managed_state_name_exists(state_dir_fd, path.name):
                raise SyncError(
                    f"published sync state disappeared during rollback: {path}"
                )
            rollback_backup, rollback_matches = (
                _move_managed_state_entry_to_quarantine(
                    home,
                    transaction,
                    state_dir_fd,
                    path.name,
                    "rollback-current",
                    expected_identity=transaction.published_identity,
                    expected_snapshot=transaction.after,
                )
            )
            transaction.published = False
            transaction.published_identity = None
            if not rollback_matches:
                raise SyncError(
                    "changed sync state was retained in quarantine during rollback: "
                    f"{path} -> {rollback_backup}"
                )
        elif transaction.backup is None:
            if _managed_state_file_matches(
                home,
                path,
                transaction.before,
                parent_fd=state_dir_fd,
            ):
                return
            raise SyncError(
                f"sync state changed before rollback and was left in place: {path}"
            )
        elif _managed_state_name_exists(state_dir_fd, path.name):
            raise SyncError(
                f"changed sync state was left in place during rollback: {path}"
            )

        if not transaction.before.exists:
            return
        if transaction.backup is None:
            raise SyncError("managed state rollback has no original backup")
        if not _managed_state_quarantine_file_matches(
            home,
            transaction,
            transaction.backup,
            transaction.before,
        ):
            raise SyncError(
                "original sync state changed and was retained in quarantine: "
                f"{path} -> {transaction.backup}"
            )

        restored_created = False
        restored_identity: tuple[int, int] | None = None
        try:
            if transaction.before_evidence is None:
                temp_name, restored_identity = _write_managed_state_temp(
                    state_dir_fd,
                    path,
                    transaction.before,
                    "restore",
                )
                _rename_noreplace_at(
                    state_dir_fd,
                    temp_name,
                    state_dir_fd,
                    path.name,
                )
            else:
                if transaction.before.file_identity is None:
                    raise SyncError("managed state before evidence has no identity")
                evidence_parent_fd = _open_directory_beneath(
                    home,
                    transaction.before_evidence.parent,
                )
                try:
                    evidence_snapshot = _read_managed_state_file_snapshot(
                        home,
                        transaction.before_evidence,
                        evidence_parent_fd,
                        expected_identity=transaction.before.file_identity,
                    )
                    if (
                        evidence_snapshot.payload != transaction.before.payload
                        or evidence_snapshot.mode != transaction.before.mode
                    ):
                        raise SyncError("managed state before evidence changed")
                    os.link(
                        transaction.before_evidence.name,
                        path.name,
                        src_dir_fd=evidence_parent_fd,
                        dst_dir_fd=state_dir_fd,
                        follow_symlinks=False,
                    )
                finally:
                    _close_fd_quietly(evidence_parent_fd)
                restored_identity = transaction.before.file_identity
            restored_created = True
            if not _managed_state_file_matches(
                home,
                path,
                transaction.before,
                restored_identity,
                parent_fd=state_dir_fd,
            ):
                raise SyncError(f"restored sync state changed before verification: {path}")
            _fsync_directory(path.parent, state_dir_fd)
            if not _canonical_managed_state_matches(
                home,
                path,
                transaction.before,
                parent_identity,
                restored_identity,
            ):
                raise SyncError(
                    f"restored sync state changed after fsync or its parent changed: {path}"
                )
            if not _managed_state_quarantine_file_matches(
                home,
                transaction,
                transaction.backup,
                transaction.before,
            ):
                raise SyncError(
                    "original sync state changed and was retained in quarantine: "
                    f"{path} -> {transaction.backup}"
                )
        except BaseException as error:
            retained_path: Path | None = None
            cleanup_error: BaseException | None = None
            if restored_created and restored_identity is not None:
                try:
                    retained_path, retained_matches = (
                        _move_managed_state_entry_to_quarantine(
                            home,
                            transaction,
                            state_dir_fd,
                            path.name,
                            "restore-error",
                            expected_identity=restored_identity,
                        )
                    )
                    if not retained_matches:
                        cleanup_error = SyncError(
                            "restored sync state changed while being quarantined"
                        )
                except (OSError, SyncError) as quarantine_error:
                    cleanup_error = quarantine_error
            if cleanup_error is not None:
                raise SyncError(
                    "managed state restore failed and its bound restored path "
                    f"could not be safely quarantined: {cleanup_error}"
                ) from error
            if retained_path is not None:
                raise SyncError(
                    f"managed state restore failed: {error}; "
                    f"retained in quarantine: {retained_path}"
                ) from error
            if isinstance(error, FileExistsError):
                raise SyncError(
                    f"refusing to overwrite changed sync state during rollback: {path}"
                ) from error
            raise

def _commit_managed_state_transaction(
    transaction: ManagedStateFileTransaction | None,
) -> None:
    if transaction is None:
        return
    # Generic commit helpers never delete evidence by pathname. Committed pending
    # batches are removed separately through their durable cleanup ticket after
    # the active transaction pointer has been cleared.


def _write_managed_state(
    home: Path,
    state: ManagedState,
    transaction: ManagedStateFileTransaction | None = None,
) -> ManagedStateFileTransaction:
    owns_transaction = transaction is None
    if transaction is None:
        transaction = _prepare_managed_state_transaction(home, state)
    expected_after = ManagedStateFileSnapshot(
        exists=True,
        payload=_managed_state_bytes(state),
        mode=0o600,
    )
    if transaction.after != expected_after:
        raise SyncError("managed state transaction payload does not match requested state")
    try:
        _apply_managed_state_transaction(home, transaction)
    except BaseException as error:
        if owns_transaction:
            try:
                _restore_managed_state_file(home, transaction)
            except (OSError, SyncError) as rollback_error:
                raise SyncError(
                    "managed state write failed and rollback was incomplete: "
                    f"{rollback_error}"
                ) from error
        raise
    if owns_transaction:
        _commit_managed_state_transaction(transaction)
    return transaction



def _current_manifest_data(home: Path, owner: str) -> ManifestData:
    sha = _current_sha(home, owner)
    if sha is None:
        return ManifestData(owner=owner, entries=[], removed_links=[])
    _ensure_safe_release_directory(
        home,
        owner,
        sha,
        allow_missing=False,
    )
    try:
        manifest = _load_installed_manifest_data(home, owner, sha)
    except SyncError as error:
        raise SyncError(
            f"current pointer references invalid release {owner}@{sha}: {error}"
        ) from error
    if manifest.owner != owner:
        raise SyncError(
            f"current manifest owner mismatch: expected {owner}, got {manifest.owner}"
        )
    return manifest


def _read_optional_symlink_target_beneath(
    home: Path,
    target: Path,
) -> str | None:
    snapshot = _read_optional_symlink_snapshot_beneath(home, target)
    return None if snapshot is None else snapshot.link_target


def _is_optional_desired_entry(entry: LinkEntry) -> bool:
    return (
        entry.owner == PUBLIC_OWNER
        and entry.target in OPTIONAL_PUBLIC_TARGETS
    )


def _refresh_managed_state_from_current(
    home: Path,
    state: ManagedState,
    *,
    bootstrap_history: bool,
) -> ManagedState:
    refreshed = ManagedState(
        owners=dict(state.owners),
        links=dict(state.links),
    )
    known_owners = _known_owners(home)
    for owner in sorted(known_owners):
        sha = _current_sha(home, owner)
        if sha is None:
            continue
        manifest = _current_manifest_data(home, owner)
        refreshed.owners[owner] = sha
        for entry in manifest.entries:
            if (
                not bootstrap_history
                and entry.target not in refreshed.links
            ):
                continue
            target = _entry_target_path(home, entry)
            desired = _desired_link_target(home, entry)
            if _read_optional_symlink_target_beneath(home, target) == desired:
                refreshed.links[entry.target] = ManagedLinkRecord(
                    source=entry.source,
                    target=entry.target,
                    kind=entry.kind,
                    owner=entry.owner,
                    link_target=desired,
                    release_sha=sha,
                )
    if bootstrap_history:
        for owner in sorted(known_owners):
            releases_root = _releases_root(home, owner)
            if not _ensure_safe_internal_directory(
                home,
                releases_root,
                create=False,
                allow_missing=True,
            ):
                continue
            for release_root in _valid_release_dirs(home, owner):
                sha = release_root.name
                manifest = _load_installed_manifest_data(home, owner, sha)
                if manifest.owner != owner:
                    continue
                for entry in manifest.entries:
                    if entry.target in refreshed.links:
                        continue
                    target = _entry_target_path(home, entry)
                    desired = _desired_link_target(home, entry)
                    if _read_optional_symlink_target_beneath(home, target) == desired:
                        refreshed.links[entry.target] = ManagedLinkRecord(
                            source=entry.source,
                            target=entry.target,
                            kind=entry.kind,
                            owner=entry.owner,
                            link_target=desired,
                            release_sha=sha,
                        )
    return refreshed


def _verify_managed_state_current_claims(
    home: Path,
    state: ManagedState,
) -> None:
    for owner, release_sha in state.owners.items():
        current = _current_link(home, owner)
        actual_snapshot = _capture_reconcile_target_snapshot(home, current)
        expected_target = f"releases/{release_sha}"
        if actual_snapshot.link_identity is None:
            continue
        if actual_snapshot.link_target != expected_target:
            raise SyncError(
                "managed state/current release mismatch for "
                f"{owner}: state={release_sha}, "
                f"current={actual_snapshot.link_target}"
            )
    for target, record in state.links.items():
        actual_snapshot = _capture_reconcile_target_snapshot(
            home,
            home / Path(*target.parts),
        )
        if actual_snapshot.link_identity is None:
            continue
        if actual_snapshot.link_target != record.link_target:
            raise SyncError(
                "managed state/link target mismatch for "
                f"{target}: state={record.link_target}, "
                f"current={actual_snapshot.link_target}"
            )


def _entries_owner(entries: list[LinkEntry]) -> str:
    owners = {entry.owner for entry in entries}
    if not owners:
        return PUBLIC_OWNER
    if len(owners) != 1:
        raise SyncError("sync manifest entries must use a single owner")
    return next(iter(owners))


def _install_lock_binding_matches(
    home: Path,
    lock_path: Path,
    parent_fd: int,
    expected_parent_identity: tuple[int, int],
    lock_fd: int,
    expected_lock_identity: tuple[int, int],
) -> bool:
    try:
        parent_metadata = os.fstat(parent_fd)
        lock_metadata = os.fstat(lock_fd)
    except OSError:
        return False
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or (parent_metadata.st_dev, parent_metadata.st_ino)
        != expected_parent_identity
        or not stat.S_ISREG(lock_metadata.st_mode)
        or (lock_metadata.st_dev, lock_metadata.st_ino) != expected_lock_identity
    ):
        return False
    if not _bound_directory_matches(home, lock_path.parent, parent_fd):
        return False
    try:
        named_metadata = os.stat(
            lock_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        return False
    if (
        not stat.S_ISREG(named_metadata.st_mode)
        or (named_metadata.st_dev, named_metadata.st_ino)
        != expected_lock_identity
    ):
        return False
    return _bound_directory_matches(home, lock_path.parent, parent_fd)


@contextlib.contextmanager
def installation_lock(home: Path):
    """Serialize cooperative installers on a stable local sync-home inode."""
    lock_path = _install_lock_path(home)
    sync_root = lock_path.parent
    home_fd = _open_or_create_sync_home(home)
    directory_fd = -1
    lock_fd = -1
    home_lock_acquired = False
    lock_acquired = False
    lock_flags = os.O_RDWR | os.O_CREAT
    lock_flags |= getattr(os, "O_CLOEXEC", 0)
    lock_flags |= getattr(os, "O_NOFOLLOW", 0)
    lock_flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        # Cooperative peers may rotate sync_root or install.lock, but the local
        # sync-home directory inode is the stable serialization anchor.
        home_identity = _directory_identity(home_fd)
        fcntl.flock(home_fd, fcntl.LOCK_EX)
        home_lock_acquired = True
        if (
            _directory_identity(home_fd) != home_identity
            or not _bound_directory_matches(home, home, home_fd)
        ):
            raise SyncError(f"install lock stable home changed: {home}")
        directory_fd = _open_or_create_directory_beneath(
            home,
            sync_root,
            mode=0o700,
            home_fd=home_fd,
        )
        parent_identity = _directory_identity(directory_fd)
        if not _bound_directory_matches(home, sync_root, directory_fd):
            raise SyncError(f"install lock parent changed before open: {sync_root}")
        try:
            lock_fd = os.open(
                lock_path.name,
                lock_flags,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise SyncError(f"refusing unsafe install lock: {lock_path}: {error}") from error
        lock_metadata = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_metadata.st_mode):
            raise SyncError(f"refusing non-file install lock: {lock_path}")
        lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_acquired = True
        if not _install_lock_binding_matches(
            home,
            lock_path,
            directory_fd,
            parent_identity,
            lock_fd,
            lock_identity,
        ):
            raise SyncError(
                f"install lock binding changed after acquisition: {lock_path}"
            )
        if not _install_lock_binding_matches(
            home,
            lock_path,
            directory_fd,
            parent_identity,
            lock_fd,
            lock_identity,
        ):
            raise SyncError(
                f"install lock binding changed before transaction: {lock_path}"
            )
        try:
            yield
        finally:
            if (
                _directory_identity(home_fd) != home_identity
                or not _bound_directory_matches(home, home, home_fd)
            ):
                raise SyncError(f"install lock stable home changed during transaction: {home}")
            if not _install_lock_binding_matches(
                home,
                lock_path,
                directory_fd,
                parent_identity,
                lock_fd,
                lock_identity,
            ):
                raise SyncError(
                    f"install lock binding changed during transaction: {lock_path}"
                )
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_acquired = False
    finally:
        if lock_acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if lock_fd >= 0:
            _close_fd_quietly(lock_fd)
        if directory_fd >= 0:
            _close_fd_quietly(directory_fd)
        if home_lock_acquired:
            try:
                fcntl.flock(home_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        _close_fd_quietly(home_fd)


def _entry_target_path(home: Path, entry: LinkEntry) -> Path:
    return home / Path(*entry.target.parts)


def _entry_current_source(home: Path, entry: LinkEntry) -> Path:
    return _current_link(home, entry.owner) / Path(*entry.source.parts)


def _desired_link_target(home: Path, entry: LinkEntry) -> str:
    target_path = _entry_target_path(home, entry)
    source_path = _entry_current_source(home, entry)
    return os.path.relpath(source_path, start=target_path.parent)


def _path_exists_or_is_link(path: Path) -> bool:
    return os.path.lexists(path)


def _ensure_safe_target_parent(home: Path, target: Path) -> None:
    try:
        relative_target = target.relative_to(home)
    except ValueError as error:
        raise SyncError(f"managed target is outside home: {target}") from error
    current = home
    for part in relative_target.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise SyncError(f"refusing managed target below symlink parent: {current}")
        if _path_exists_or_is_link(current) and not current.is_dir():
            raise SyncError(f"link parent exists but is not a directory: {current}")


def _known_owners(home: Path, extra_owners: set[str] | None = None) -> set[str]:
    owners = {PUBLIC_OWNER}
    if extra_owners:
        owners.update(extra_owners)
    overlays_root = _personal_sync_root(home) / "overlays"
    if not _ensure_safe_internal_directory(
        home,
        overlays_root,
        create=False,
        allow_missing=True,
    ):
        return owners
    for path in overlays_root.iterdir():
        if OWNER_RE.fullmatch(path.name) is None:
            continue
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SyncError(f"refusing unsafe overlay directory: {path}")
        owners.add(path.name)
    return owners


def _link_managed_owner(home: Path, link: Path, owners: set[str] | None = None) -> str | None:
    link_target = _read_symlink_beneath(home, link)
    linked_path = (link.parent / link_target).resolve(strict=False)
    for owner in sorted(
        _known_owners(home, owners),
        key=lambda value: (value != PUBLIC_OWNER, value),
    ):
        releases_root = _releases_root(home, owner).resolve(strict=False)
        try:
            linked_path.relative_to(releases_root)
        except ValueError:
            continue
        return owner
    return None


def _removed_link_target(home: Path, removed: RemovedLink) -> str:
    entry = LinkEntry(
        source=removed.source,
        target=removed.target,
        kind=removed.kind,
        owner=removed.owner,
    )
    return _desired_link_target(home, entry)


def _combine_entries(
    public_entries: list[LinkEntry],
    overlay_manifests: list[ManifestData],
) -> list[LinkEntry]:
    final_by_target: dict[PurePosixPath, LinkEntry] = {}
    for entry in public_entries:
        if entry.owner != PUBLIC_OWNER:
            raise SyncError("public base manifest must contain only public-owned entries")
        final_by_target[entry.target] = entry

    for manifest in overlay_manifests:
        if manifest.owner == PUBLIC_OWNER:
            raise SyncError("overlay manifest owner must not be public")
        for entry in manifest.entries:
            existing = final_by_target.get(entry.target)
            if existing is not None and existing.owner != PUBLIC_OWNER:
                raise SyncError(
                    f"target {_display_path(Path(*entry.target.parts))} is declared by "
                    f"multiple overlays: {existing.owner}, {entry.owner}"
                )
            if existing is not None:
                if not entry.override and entry.target not in OPTIONAL_PUBLIC_TARGETS:
                    raise SyncError(
                        f"overlay target {entry.target} must declare override=true"
                    )
            elif entry.override and entry.target not in OPTIONAL_PUBLIC_TARGETS:
                raise SyncError(f"override target has no public base target: {entry.target}")
            final_by_target[entry.target] = entry
    final_entries = list(final_by_target.values())
    _validate_non_overlapping_targets([entry.target for entry in final_entries])
    return final_entries


def _combine_removed_links(manifests: list[ManifestData]) -> list[RemovedLink]:
    removed_links: list[RemovedLink] = []
    keys: set[str] = set()
    for manifest in manifests:
        for removed in manifest.removed_links:
            key = _removed_link_key(removed)
            if key in keys:
                raise SyncError(f"duplicate removed link key: {key}")
            keys.add(key)
            removed_links.append(removed)
    return removed_links


def _validate_replacement_retirement_graph(
    graph: dict[str, tuple[str, ...]],
) -> None:
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        cycle_keys = error.args[1] if len(error.args) > 1 else ()
        cycle = " -> ".join(str(key) for key in cycle_keys)
        detail = f": {cycle}" if cycle else ""
        raise SyncError(f"replacement retirement cycle detected{detail}") from None


def _validate_active_replacements(
    _home: Path,
    _current_manifests: dict[str, ManifestData],
    next_manifests: dict[str, ManifestData],
    desired_entries: list[LinkEntry],
) -> None:
    desired_targets = _entries_by_target(desired_entries)
    all_removed = [
        removed
        for manifest in next_manifests.values()
        for removed in manifest.removed_links
    ]
    removed_by_key = {_removed_link_key(removed): removed for removed in all_removed}
    for retirement in all_removed:
        retirement_key = _removed_link_key(retirement)
        for retired_key in retirement.retires_replacements:
            if retired_key == retirement_key:
                raise SyncError(
                    f"removed link {retirement_key} must not retire its own replacement"
                )
            retired = removed_by_key.get(retired_key)
            if retired is None:
                retired_owner, _retired_id = retired_key.split(":", 1)
                if retired_owner not in next_manifests:
                    continue
                raise SyncError(
                    f"removed link {_removed_link_key(retirement)} retires unknown "
                    f"replacement {retired_key}"
                )
            if retired.replacement_target != retirement.target:
                raise SyncError(
                    f"removed link {_removed_link_key(retirement)} target "
                    f"{retirement.target} does not match replacement for {retired_key}"
                )
    retirement_graph = {
        key: tuple(
            retired_key
            for retired_key in removed.retires_replacements
            if retired_key in removed_by_key
        )
        for key, removed in removed_by_key.items()
    }
    _validate_replacement_retirement_graph(retirement_graph)
    for owner, manifest in next_manifests.items():
        for removed in manifest.removed_links:
            replacement = removed.replacement_target
            if replacement is None:
                continue
            replacement_has_removal_history = any(
                _removed_link_key(removed) in candidate.retires_replacements
                for candidate in all_removed
            )
            if (
                replacement not in desired_targets
                and not replacement_has_removal_history
            ):
                raise SyncError(
                    f"replacement target {replacement} is unavailable for active removal "
                    f"{owner}:{removed.id}"
                )


def _required_replacements_for_removals(
    home: Path,
    actions: list[ReconcileAction],
    removed_links: list[RemovedLink],
    desired_entries: list[LinkEntry],
) -> dict[Path, list[LinkEntry]]:
    destructive_actions: dict[PurePosixPath, list[ReconcileAction]] = {}
    for action in actions:
        if action.action not in {
            "replace",
            "quarantine-replace",
            "remove",
            "quarantine-remove",
        }:
            continue
        try:
            relative_target = action.target.relative_to(home)
        except ValueError as error:
            raise SyncError(f"managed target is outside home: {action.target}") from error
        destructive_actions.setdefault(
            PurePosixPath(*relative_target.parts),
            [],
        ).append(action)

    desired_by_target = _entries_by_target(desired_entries)
    retired_keys = {
        retired_key
        for retirement in removed_links
        for retired_key in retirement.retires_replacements
    }
    required: dict[Path, dict[PurePosixPath, LinkEntry]] = {}
    for removed in removed_links:
        removed_key = _removed_link_key(removed)
        replacement = removed.replacement_target
        if replacement is None or removed_key in retired_keys:
            continue
        matching_actions = [
            action
            for action in destructive_actions.get(removed.target, [])
            if action.expected_link_target == _removed_link_target(home, removed)
        ]
        if not matching_actions:
            continue
        replacement_entry = desired_by_target.get(replacement)
        if replacement_entry is None:
            raise SyncError(
                f"replacement target {replacement} is unavailable for active removal "
                f"{removed_key}"
            )
        for action in matching_actions:
            required.setdefault(action.target, {})[replacement] = replacement_entry
    return {
        action_target: [
            entries[target]
            for target in sorted(entries, key=PurePosixPath.as_posix)
        ]
        for action_target, entries in required.items()
    }


def _plan_reconciliation(
    home: Path,
    desired_entries: list[LinkEntry],
    previous_entries: list[LinkEntry],
    removed_links: list[RemovedLink],
    state: ManagedState,
    *,
    allow_cross_owner: bool,
) -> list[ReconcileAction]:
    desired_by_target = _entries_by_target(desired_entries)
    previous_targets: dict[PurePosixPath, set[str]] = {}
    for entry in previous_entries:
        previous_targets.setdefault(entry.target, set()).add(_desired_link_target(home, entry))
    removed_by_target: dict[PurePosixPath, list[RemovedLink]] = {}
    for removed in removed_links:
        removed_by_target.setdefault(removed.target, []).append(removed)

    candidate_targets = set(desired_by_target)
    candidate_targets.update(state.links)
    candidate_targets.update(previous_targets)
    candidate_targets.update(removed_by_target)
    actions: list[ReconcileAction] = []

    for relative_target in sorted(candidate_targets, key=PurePosixPath.as_posix):
        target = home / Path(*relative_target.parts)
        desired_entry = desired_by_target.get(relative_target)
        if desired_entry is None and any(
            ancestor in desired_by_target for ancestor in relative_target.parents
        ):
            continue
        _ensure_safe_target_parent(home, target)
        planned_snapshot = (
            _capture_reconcile_target_snapshot(home, target)
            if home.is_dir() and not home.is_symlink()
            else None
        )
        record = state.links.get(relative_target)
        removed_candidates = removed_by_target.get(relative_target, [])
        if desired_entry is not None:
            desired = _desired_link_target(home, desired_entry)
            if planned_snapshot is None or planned_snapshot.link_identity is None:
                actions.append(
                    ReconcileAction(
                        "create",
                        target,
                        desired,
                        desired_entry.kind,
                        planned_snapshot=planned_snapshot,
                    )
                )
                continue
            if planned_snapshot.link_target is None:
                if (
                    desired_entry.owner == PUBLIC_OWNER
                    and desired_entry.target in OPTIONAL_PUBLIC_TARGETS
                ):
                    continue
                raise SyncError(f"refusing to replace non-symlink target: {target}")
            assert planned_snapshot.link_target is not None
            existing = planned_snapshot.link_target
            if existing == desired:
                continue

            proven = record is not None and record.link_target == existing
            removed_match = next(
                (
                    removed
                    for removed in removed_candidates
                    if _removed_link_target(home, removed) == existing
                ),
                None,
            )
            if proven:
                actions.append(
                    ReconcileAction(
                        "replace",
                        target,
                        desired,
                        desired_entry.kind,
                        expected_link_target=existing,
                        planned_snapshot=planned_snapshot,
                    )
                )
                continue
            if removed_match is not None:
                if removed_match.owner != desired_entry.owner and not allow_cross_owner:
                    raise SyncError(
                        f"cross-owner migration requires install-private: {target}"
                    )
                actions.append(
                    ReconcileAction(
                        "quarantine-replace",
                        target,
                        desired,
                        desired_entry.kind,
                        expected_link_target=existing,
                        removed_link_key=_removed_link_key(removed_match),
                        planned_snapshot=planned_snapshot,
                    )
                )
                continue
            if (
                desired_entry.owner == PUBLIC_OWNER
                and desired_entry.target in OPTIONAL_PUBLIC_TARGETS
            ):
                continue
            raise SyncError(f"refusing to replace unproven symlink target: {target}")

        if planned_snapshot is None or (
            planned_snapshot.link_identity is None
            or planned_snapshot.link_target is None
        ):
            continue
        assert planned_snapshot.link_target is not None
        existing = planned_snapshot.link_target
        if record is not None and record.link_target == existing:
            actions.append(
                ReconcileAction(
                    "remove",
                    target,
                    "",
                    record.kind,
                    expected_link_target=existing,
                    planned_snapshot=planned_snapshot,
                )
            )
            continue
        removed_match = next(
            (
                removed
                for removed in removed_candidates
                if _removed_link_target(home, removed) == existing
            ),
            None,
        )
        if removed_match is not None:
            actions.append(
                ReconcileAction(
                    "quarantine-remove",
                    target,
                    "",
                    removed_match.kind,
                    expected_link_target=existing,
                    removed_link_key=_removed_link_key(removed_match),
                    planned_snapshot=planned_snapshot,
                )
            )
    return actions


def _quarantine_batch_root(home: Path, actions: list[ReconcileAction]) -> Path:
    quarantine_root = _personal_sync_root(home) / QUARANTINE_RELATIVE_PATH
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_name = f"{stamp}-{os.getpid()}-{time.time_ns()}"
    if len(batch_name.encode("utf-8")) > MAX_PENDING_LINK_BATCH_NAME_BYTES:
        raise SyncError("quarantine batch name exceeds the size limit")
    batch_root = quarantine_root / batch_name
    metadata = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actions": [
            {
                "action": action.action,
                "target": str(action.target),
                "link_target": action.expected_link_target,
                "replacement": action.link_target or None,
                "removed_link": action.removed_link_key,
            }
            for action in actions
        ],
    }
    quarantine_fd = _open_or_create_directory_beneath(
        home,
        quarantine_root,
        mode=0o700,
    )
    batch_fd = -1
    metadata_fd = -1
    created_batch = False
    try:
        if not _bound_directory_matches(home, quarantine_root, quarantine_fd):
            raise SyncError(f"quarantine root changed: {quarantine_root}")
        retained_batches = 0
        with os.scandir(quarantine_fd) as iterator:
            for scanned, entry in enumerate(iterator, start=1):
                if scanned > MAX_PENDING_CLEANUP_BATCH_SCAN:
                    raise SyncError("quarantine batch scan exceeds the size limit")
                if (
                    _pending_cleanup_batch_name_from_quarantine_entry(
                        entry.name
                    )
                    is not None
                    and entry.is_dir(follow_symlinks=False)
                ):
                    retained_batches += 1
        if retained_batches >= MAX_RETAINED_QUARANTINE_BATCHES:
            raise SyncError(
                "quarantine retains too many transaction batches: "
                f"{retained_batches} >= {MAX_RETAINED_QUARANTINE_BATCHES}"
            )
        os.mkdir(batch_root.name, mode=0o700, dir_fd=quarantine_fd)
        created_batch = True
        os.fsync(quarantine_fd)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        batch_fd = os.open(batch_root.name, directory_flags, dir_fd=quarantine_fd)
        metadata_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        metadata_flags |= getattr(os, "O_CLOEXEC", 0)
        metadata_flags |= getattr(os, "O_NOFOLLOW", 0)
        metadata_fd = os.open(
            "metadata.json",
            metadata_flags,
            0o600,
            dir_fd=batch_fd,
        )
        with os.fdopen(metadata_fd, "w", encoding="utf-8", closefd=True) as file:
            metadata_fd = -1
            json.dump(metadata, file, indent=2, sort_keys=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.fsync(batch_fd)
        if not _bound_directory_matches(home, batch_root, batch_fd):
            raise SyncError(f"quarantine batch changed: {batch_root}")
    except BaseException:
        if metadata_fd >= 0:
            _close_fd_quietly(metadata_fd)
        if batch_fd >= 0:
            try:
                os.unlink("metadata.json", dir_fd=batch_fd)
            except OSError:
                pass
        if created_batch:
            try:
                os.rmdir(batch_root.name, dir_fd=quarantine_fd)
                os.fsync(quarantine_fd)
            except OSError:
                pass
        raise
    finally:
        if batch_fd >= 0:
            _close_fd_quietly(batch_fd)
        _close_fd_quietly(quarantine_fd)
    return batch_root


def _snapshot_payload_digest(snapshot: ManagedStateFileSnapshot) -> str | None:
    if not snapshot.exists:
        return None
    assert snapshot.payload is not None
    return hashlib.sha256(snapshot.payload).hexdigest()


def _identity_payload(identity: tuple[int, int] | None) -> list[int] | None:
    return list(identity) if identity is not None else None


def _planned_snapshot_payload(snapshot: ReconcileTargetSnapshot) -> dict[str, Any]:
    return {
        "parent_identity": _identity_payload(snapshot.parent_identity),
        "link_identity": _identity_payload(snapshot.link_identity),
        "link_target": snapshot.link_target,
        "ancestor_identity": _identity_payload(snapshot.ancestor_identity),
        "missing_parent_parts": list(snapshot.missing_parent_parts),
    }


def _state_evidence_payload(
    snapshot: ManagedStateFileSnapshot,
    evidence: PurePosixPath | None,
) -> dict[str, Any]:
    return {
        "exists": snapshot.exists,
        "mode": snapshot.mode if snapshot.exists else None,
        "sha256": _snapshot_payload_digest(snapshot),
        "identity": _identity_payload(snapshot.file_identity),
        "evidence": evidence.as_posix() if evidence is not None else None,
    }


def _pending_claim_payload(claim: PendingLinkClaim) -> dict[str, Any]:
    return {
        "index": claim.index,
        "scope": claim.scope,
        "target": claim.target.as_posix(),
        "kind": claim.kind,
        "source": claim.source.as_posix() if claim.source is not None else None,
        "owner": claim.owner,
        "link_target": claim.link_target,
        "release_sha": claim.release_sha,
        "parent_identity": _identity_payload(claim.parent_identity),
        "link_identity": _identity_payload(claim.link_identity),
        "evidence": claim.evidence.as_posix(),
    }


def _pending_release_payload(
    expectation: PendingReleaseExpectation,
) -> dict[str, Any]:
    return {
        "owner": expectation.owner,
        "sha": expectation.sha,
        "directory_identity": _identity_payload(
            expectation.directory_identity
        ),
        "tree_sha256": expectation.tree_sha256,
    }


def _pending_commit_evidence_payload(
    batch_root: Path,
    state_after: ManagedStateFileSnapshot,
) -> bytes:
    if (
        state_after.parent_identity is None
        or state_after.file_identity is None
        or state_after.payload is None
    ):
        raise SyncError("pending state-after evidence is not fully bound")
    payload = {
        "version": 1,
        "batch": batch_root.name,
        "state_parent_identity": _identity_payload(
            state_after.parent_identity
        ),
        "state_after_identity": _identity_payload(state_after.file_identity),
        "state_after_sha256": hashlib.sha256(state_after.payload).hexdigest(),
    }
    return (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _bounded_json_document(
    payload: object,
    *,
    max_bytes: int,
    overflow_error: str,
) -> bytes:
    encoded = bytearray()
    encoder = json.JSONEncoder(indent=2, sort_keys=False)
    try:
        for chunk in encoder.iterencode(payload):
            raw_chunk = chunk.encode("utf-8")
            if len(encoded) + len(raw_chunk) + 1 > max_bytes:
                raise SyncError(overflow_error)
            encoded.extend(raw_chunk)
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as error:
        raise SyncError(f"failed to encode bounded JSON document: {error}") from error
    if len(encoded) + 1 > max_bytes:
        raise SyncError(overflow_error)
    encoded.extend(b"\n")
    return bytes(encoded)


def _pending_link_metadata_payload(
    batch_root: Path,
    records: tuple[PendingLinkRecord, ...],
    claims_before: tuple[PendingLinkClaim, ...],
    claims_after: tuple[PendingLinkClaim, ...],
    state_before: ManagedStateFileSnapshot,
    state_after: ManagedStateFileSnapshot,
    state_before_evidence: PurePosixPath | None,
    state_after_evidence: PurePosixPath,
    releases_before: tuple[PendingReleaseExpectation, ...],
    releases_after: tuple[PendingReleaseExpectation, ...],
    commit_evidence: ManagedStateFileSnapshot,
) -> bytes:
    payload = {
        "version": 4,
        "batch": batch_root.name,
        "state_parent_identity": _identity_payload(state_before.parent_identity),
        "state_before": _state_evidence_payload(
            state_before,
            state_before_evidence,
        ),
        "state_after": _state_evidence_payload(state_after, state_after_evidence),
        "releases_before": [
            _pending_release_payload(expectation)
            for expectation in releases_before
        ],
        "releases_after": [
            _pending_release_payload(expectation)
            for expectation in releases_after
        ],
        "commit_evidence": _state_evidence_payload(
            commit_evidence,
            PENDING_STATE_COMMIT_EVIDENCE,
        ),
        "commit_marker": PENDING_STATE_COMMIT_MARKER.as_posix(),
        "claims_before": [
            _pending_claim_payload(claim) for claim in claims_before
        ],
        "claims_after": [
            _pending_claim_payload(claim) for claim in claims_after
        ],
        "records": [
            {
                "index": record.index,
                "scope": record.scope,
                "action": record.action,
                "target": record.target.as_posix(),
                "kind": record.kind,
                "planned_before": _planned_snapshot_payload(
                    record.planned_snapshot
                ),
                "source": record.source.as_posix() if record.source is not None else None,
                "owner": record.owner,
                "link_target": record.link_target,
                "release_sha": record.release_sha,
                "before_evidence": (
                    record.before_evidence.as_posix()
                    if record.before_evidence is not None
                    else None
                ),
                "before_evidence_identity": _identity_payload(
                    record.before_evidence_identity
                ),
                "backup": record.backup.as_posix() if record.backup is not None else None,
                "stage": record.stage.as_posix() if record.stage is not None else None,
                "stage_identity": _identity_payload(record.stage_identity),
                "evidence": (
                    record.evidence.as_posix() if record.evidence is not None else None
                ),
                "evidence_identity": _identity_payload(record.evidence_identity),
            }
            for record in records
        ],
    }
    return _bounded_json_document(
        payload,
        max_bytes=MAX_MANAGED_STATE_BYTES,
        overflow_error="pending link transaction metadata exceeds the size limit",
    )


def _write_exclusive_internal_file(
    home: Path,
    path: Path,
    payload: bytes,
) -> ManagedStateFileSnapshot:
    parent_fd = _open_directory_beneath(home, path.parent)
    file_fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        with os.fdopen(file_fd, "wb", closefd=True) as file:
            file_fd = -1
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.fsync(parent_fd)
        return _read_managed_state_file_snapshot(home, path, parent_fd)
    finally:
        if file_fd >= 0:
            _close_fd_quietly(file_fd)
        _close_fd_quietly(parent_fd)


def _publish_regular_hardlink_beneath(
    home: Path,
    source: Path,
    destination: Path,
    expected_source: ManagedStateFileSnapshot,
) -> ManagedStateFileSnapshot:
    if not expected_source.exists or expected_source.file_identity is None:
        raise SyncError(f"regular-file evidence has no source identity: {source}")
    source_parent_fd = _open_directory_beneath(home, source.parent)
    destination_parent_fd = _open_directory_beneath(home, destination.parent)
    published = False
    try:
        source_snapshot = _read_managed_state_file_snapshot(
            home,
            source,
            source_parent_fd,
            expected_identity=expected_source.file_identity,
        )
        if source_snapshot != expected_source:
            raise SyncError(f"regular-file evidence source changed: {source}")
        os.link(
            source.name,
            destination.name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
        published = True
        destination_snapshot = _read_managed_state_file_snapshot(
            home,
            destination,
            destination_parent_fd,
            expected_identity=expected_source.file_identity,
        )
        if (
            destination_snapshot.file_identity != expected_source.file_identity
            or destination_snapshot.payload != expected_source.payload
            or destination_snapshot.mode != expected_source.mode
        ):
            raise SyncError(f"regular-file evidence changed: {destination}")
        os.fsync(destination_parent_fd)
        if (
            not _bound_directory_matches(home, source.parent, source_parent_fd)
            or not _bound_directory_matches(
                home,
                destination.parent,
                destination_parent_fd,
            )
        ):
            raise SyncError(f"regular-file evidence parent changed: {destination}")
        return destination_snapshot
    except BaseException as error:
        if published:
            raise SyncError(
                "regular-file evidence publication failed after publication; "
                f"the unreferenced evidence was retained: {destination}"
            ) from error
        raise
    finally:
        _close_fd_quietly(destination_parent_fd)
        _close_fd_quietly(source_parent_fd)


def _pending_current_owner(
    home: Path,
    target: Path,
    owner_shas: dict[str, str],
    state_before_value: ManagedState,
) -> str:
    candidates = set(owner_shas) | set(state_before_value.owners)
    matches = sorted(owner for owner in candidates if _current_link(home, owner) == target)
    if len(matches) != 1:
        raise SyncError(f"pending current action has ambiguous owner: {target}")
    return matches[0]


def _managed_state_value_from_snapshot(
    home: Path,
    snapshot: ManagedStateFileSnapshot,
    manifest_entry_indexes: (
        dict[
            tuple[str, str],
            dict[tuple[PurePosixPath, PurePosixPath, str, str], LinkEntry],
        ]
        | None
    ) = None,
) -> ManagedState:
    if not snapshot.exists:
        return _empty_managed_state()
    assert snapshot.payload is not None
    return _managed_state_from_payload(
        home,
        _decode_managed_state_json(snapshot.payload, _state_path(home)),
        manifest_entry_indexes,
    )


def _pending_state_claim_semantics(
    home: Path,
    state: ManagedState,
) -> list[tuple[str, PurePosixPath, str, PurePosixPath | None, str, str, str]]:
    claims: list[
        tuple[str, PurePosixPath, str, PurePosixPath | None, str, str, str]
    ] = []
    for owner, release_sha in state.owners.items():
        target = PurePosixPath(*_current_link(home, owner).relative_to(home).parts)
        claims.append(
            (
                "current",
                target,
                "directory",
                None,
                owner,
                f"releases/{release_sha}",
                release_sha,
            )
        )
    for record in state.links.values():
        claims.append(
            (
                "managed",
                record.target,
                record.kind,
                record.source,
                record.owner,
                record.link_target,
                record.release_sha,
            )
        )
    return sorted(claims, key=lambda claim: (claim[0], claim[1].as_posix()))


def _pending_record_has_bound_create_absence(
    record: PendingLinkRecord,
) -> bool:
    snapshot = record.planned_snapshot
    return (
        record.action == "create"
        and snapshot.parent_identity is not None
        and snapshot.link_identity is None
        and snapshot.link_target is None
        and snapshot.ancestor_identity == snapshot.parent_identity
        and not snapshot.missing_parent_parts
    )


def _require_pending_record_bound_create_absence(
    record: PendingLinkRecord,
) -> None:
    if not _pending_record_has_bound_create_absence(record):
        raise SyncError(
            f"pending create target does not have a bound absence: {record.target}"
        )


def _verify_pending_record_bound_create_absence(
    home: Path,
    record: PendingLinkRecord,
    *,
    phase: str,
) -> None:
    _require_pending_record_bound_create_absence(record)
    target = home / Path(*record.target.parts)
    try:
        actual = _capture_reconcile_target_snapshot(home, target)
    except (OSError, SyncError) as error:
        raise SyncError(
            f"pending {phase} create absence could not be verified: {record.target}"
        ) from error
    if actual != record.planned_snapshot:
        raise SyncError(
            f"pending {phase} create absence changed: {record.target}"
        )


def _pending_record_has_bound_retired_absence(
    record: PendingLinkRecord,
) -> bool:
    snapshot = record.planned_snapshot
    if (
        record.scope not in {"current", "managed"}
        or record.action != "retire-absent"
        or snapshot.link_identity is not None
        or snapshot.link_target is not None
    ):
        return False
    if snapshot.parent_identity is not None:
        return (
            snapshot.ancestor_identity == snapshot.parent_identity
            and not snapshot.missing_parent_parts
        )
    return (
        snapshot.ancestor_identity is not None
        and bool(snapshot.missing_parent_parts)
    )


def _require_pending_record_bound_retired_absence(
    record: PendingLinkRecord,
) -> None:
    if not _pending_record_has_bound_retired_absence(record):
        raise SyncError(
            f"pending retired target does not have a bound absence: {record.target}"
        )


def _verify_pending_record_bound_retired_absence(
    home: Path,
    record: PendingLinkRecord,
    *,
    phase: str,
) -> None:
    _require_pending_record_bound_retired_absence(record)
    target = home / Path(*record.target.parts)
    try:
        actual = _capture_reconcile_target_snapshot(home, target)
    except (OSError, SyncError) as error:
        raise SyncError(
            f"pending {phase} retired absence could not be verified: {record.target}"
        ) from error
    if actual != record.planned_snapshot:
        raise SyncError(
            f"pending {phase} retired absence changed: {record.target}"
        )


def _stage_pending_link_claims(
    home: Path,
    batch_root: Path,
    phase: str,
    state: ManagedState,
    records: tuple[PendingLinkRecord, ...],
    created_parent_identities: dict[Path, tuple[int, int]],
) -> tuple[PendingLinkClaim, ...]:
    if phase not in {"before", "after"}:
        raise SyncError(f"unsupported pending claim phase: {phase}")
    record_by_target = {
        (record.scope, record.target): record for record in records
    }
    claims: list[PendingLinkClaim] = []
    for semantic in _pending_state_claim_semantics(home, state):
        (
            scope,
            target,
            kind,
            source,
            owner,
            link_target,
            release_sha,
        ) = semantic
        record = record_by_target.get((scope, target))
        if phase == "before" and record is not None and record.action in {
            "create",
            "retire-absent",
        }:
            if record.action == "create":
                _verify_pending_record_bound_create_absence(
                    home,
                    record,
                    phase="before-state",
                )
            else:
                _verify_pending_record_bound_retired_absence(
                    home,
                    record,
                    phase="before-state",
                )
            continue
        canonical_target = home / Path(*target.parts)
        if phase == "after" and record is not None:
            if record.action not in {"create", "replace", "quarantine-replace"}:
                raise SyncError(
                    f"pending after-state still claims a removed target: {target}"
                )
            if (
                record.kind != kind
                or record.source != source
                or record.owner != owner
                or record.link_target != link_target
                or record.release_sha != release_sha
                or record.evidence is None
            ):
                raise SyncError(f"pending after-state claim changed: {target}")
            source_path = batch_root / Path(*record.evidence.parts)
            source_snapshot = _read_symlink_snapshot_beneath(home, source_path)
            parent_identity = record.planned_snapshot.parent_identity
            if parent_identity is None:
                raise SyncError(f"pending after-state parent is unbound: {target}")
        else:
            source_path = canonical_target
            source_snapshot = _read_symlink_snapshot_beneath(home, source_path)
            parent_identity = source_snapshot.parent_identity
            if phase == "before" and record is not None:
                if not _symlink_snapshot_matches(
                    source_snapshot,
                    record.planned_snapshot.parent_identity,
                    record.planned_snapshot.link_identity,
                    record.planned_snapshot.link_target,
                ):
                    raise SyncError(
                        f"pending before-state claim changed after planning: {target}"
                    )
        if source_snapshot.link_target != link_target:
            raise SyncError(f"pending {phase}-state claim target changed: {target}")
        index = len(claims)
        evidence = PurePosixPath("pending", "claims", phase, f"{index:08d}")
        evidence_path = batch_root / Path(*evidence.parts)
        evidence_plan = _capture_reconcile_target_snapshot(home, evidence_path)
        evidence_snapshot = _publish_symlink_hardlink_beneath(
            home,
            source_path,
            evidence_path,
            source_snapshot,
            evidence_plan,
            created_parent_identities,
        )
        if evidence_snapshot.link_identity != source_snapshot.link_identity:
            raise SyncError(f"pending {phase}-state claim evidence changed: {target}")
        claims.append(
            PendingLinkClaim(
                index=index,
                scope=scope,
                target=target,
                kind=kind,
                source=source,
                owner=owner,
                link_target=link_target,
                release_sha=release_sha,
                parent_identity=parent_identity,
                link_identity=source_snapshot.link_identity,
                evidence=evidence,
            )
        )
    return tuple(claims)


def _pending_link_record_for_action(
    home: Path,
    scope: str,
    action: ReconcileAction,
    desired_by_target: dict[PurePosixPath, LinkEntry],
    owner_shas: dict[str, str],
    current_owner_state: ManagedState,
    index: int,
) -> PendingLinkRecord:
    if scope not in {"current", "managed"}:
        raise SyncError(f"unsupported pending transaction scope: {scope}")
    if action.planned_snapshot is None:
        raise SyncError(f"pending action has no planning snapshot: {action.target}")
    try:
        relative_target = action.target.relative_to(home)
    except ValueError as error:
        raise SyncError(f"pending target is outside home: {action.target}") from error
    target = PurePosixPath(*relative_target.parts)
    producing = action.action in {"create", "replace", "quarantine-replace"}
    destructive = action.action in {
        "replace",
        "quarantine-replace",
        "remove",
        "quarantine-remove",
    }
    if not producing and not destructive:
        raise SyncError(f"unsupported pending action: {action.action}")
    source: PurePosixPath | None = None
    owner: str | None = None
    release_sha: str | None = None
    link_target = action.link_target if producing else None
    if scope == "managed" and producing:
        entry = desired_by_target.get(target)
        if entry is None:
            raise SyncError(f"pending managed action has no desired entry: {target}")
        source = entry.source
        owner = entry.owner
        release_sha = owner_shas.get(owner)
        if release_sha is None:
            raise SyncError(f"pending managed action has no release SHA: {owner}")
        release_sha = _validate_release_sha(release_sha)
        if (
            entry.target != target
            or entry.kind != action.kind
            or _desired_link_target(home, entry) != action.link_target
        ):
            raise SyncError(f"pending managed action changed after planning: {target}")
    elif scope == "current":
        owner = _pending_current_owner(
            home,
            action.target,
            owner_shas,
            current_owner_state,
        )
        if producing:
            parts = action.link_target.split("/")
            if len(parts) != 2 or parts[0] != "releases":
                raise SyncError(f"pending current target is invalid: {action.link_target}")
            release_sha = _validate_release_sha(parts[1])
            if owner_shas.get(owner) != release_sha:
                raise SyncError(f"pending current release SHA changed for owner {owner}")

    leaf = f"{index:08d}"
    return PendingLinkRecord(
        index=index,
        scope=scope,
        action=action.action,
        target=target,
        kind=action.kind,
        planned_snapshot=action.planned_snapshot,
        source=source,
        owner=owner,
        link_target=link_target,
        release_sha=release_sha,
        before_evidence=(
            PurePosixPath("pending", "before", leaf) if destructive else None
        ),
        before_evidence_identity=(
            action.planned_snapshot.link_identity if destructive else None
        ),
        backup=(PurePosixPath("links") / target if destructive else None),
        stage=(PurePosixPath("pending", "stage", leaf) if producing else None),
        stage_identity=None,
        evidence=(PurePosixPath("pending", "evidence", leaf) if producing else None),
        evidence_identity=None,
    )


def _projected_pending_snapshot_payload(
    home: Path,
    target: Path,
    snapshot: ReconcileTargetSnapshot | None,
    *link_targets: str | None,
) -> dict[str, Any]:
    relative_parent = target.parent.relative_to(home)
    candidate_targets = [
        snapshot.link_target if snapshot is not None else None,
        *link_targets,
    ]
    link_target = max(
        candidate_targets,
        key=lambda value: len(json.dumps(value, sort_keys=False).encode("utf-8")),
    )
    return {
        "parent_identity": _identity_payload(_MAX_PENDING_IDENTITY),
        "link_identity": _identity_payload(_MAX_PENDING_IDENTITY),
        "link_target": link_target,
        "ancestor_identity": _identity_payload(_MAX_PENDING_IDENTITY),
        "missing_parent_parts": list(relative_parent.parts),
    }


def _projected_pending_record_payload(
    home: Path,
    scope: str,
    action: ReconcileAction,
    state_before_value: ManagedState,
    planning_state_before: ManagedState,
    state_after_value: ManagedState,
    index: int,
) -> dict[str, Any]:
    target = PurePosixPath(*action.target.relative_to(home).parts)
    producing = action.action in {"create", "replace", "quarantine-replace"}
    destructive = action.action in {
        "replace",
        "quarantine-replace",
        "remove",
        "quarantine-remove",
    }
    source: PurePosixPath | None = None
    owner: str | None = None
    release_sha: str | None = None
    if scope == "managed" and producing:
        record = state_after_value.links.get(target)
        if record is None:
            raise SyncError(f"pending managed action has no projected state: {target}")
        source = record.source
        owner = record.owner
        release_sha = record.release_sha
    elif scope == "current":
        owner = _pending_current_owner(
            home,
            action.target,
            state_after_value.owners,
            planning_state_before,
        )
        if producing:
            release_sha = state_after_value.owners.get(owner)
            if release_sha is None:
                raise SyncError(
                    f"pending current action has no projected release: {owner}"
                )
    leaf = f"{index:08d}"
    return {
        "index": index,
        "scope": scope,
        "action": action.action,
        "target": target.as_posix(),
        "kind": action.kind,
        "planned_before": _projected_pending_snapshot_payload(
            home,
            action.target,
            action.planned_snapshot,
            action.expected_link_target,
            action.link_target,
        ),
        "source": source.as_posix() if source is not None else None,
        "owner": owner,
        "link_target": action.link_target if producing else None,
        "release_sha": release_sha if producing else None,
        "before_evidence": (
            PurePosixPath("pending", "before", leaf).as_posix()
            if destructive
            else None
        ),
        "before_evidence_identity": (
            _identity_payload(_MAX_PENDING_IDENTITY) if destructive else None
        ),
        "backup": (
            (PurePosixPath("links") / target).as_posix()
            if destructive
            else None
        ),
        "stage": (
            PurePosixPath("pending", "stage", leaf).as_posix()
            if producing
            else None
        ),
        "stage_identity": (
            _identity_payload(_MAX_PENDING_IDENTITY) if producing else None
        ),
        "evidence": (
            PurePosixPath("pending", "evidence", leaf).as_posix()
            if producing
            else None
        ),
        "evidence_identity": (
            _identity_payload(_MAX_PENDING_IDENTITY) if producing else None
        ),
    }


def _projected_retired_absence_record_payload(
    home: Path,
    target: PurePosixPath,
    state_record: ManagedLinkRecord,
    index: int,
) -> dict[str, Any]:
    return {
        "index": index,
        "scope": "managed",
        "action": "retire-absent",
        "target": target.as_posix(),
        "kind": state_record.kind,
        "planned_before": _projected_pending_snapshot_payload(
            home,
            home / Path(*target.parts),
            None,
        ),
        "source": None,
        "owner": None,
        "link_target": None,
        "release_sha": None,
        "before_evidence": None,
        "before_evidence_identity": None,
        "backup": None,
        "stage": None,
        "stage_identity": None,
        "evidence": None,
        "evidence_identity": None,
    }


def _projected_retired_current_absence_record_payload(
    home: Path,
    target: PurePosixPath,
    owner: str,
    index: int,
) -> dict[str, Any]:
    return {
        "index": index,
        "scope": "current",
        "action": "retire-absent",
        "target": target.as_posix(),
        "kind": "directory",
        "planned_before": _projected_pending_snapshot_payload(
            home,
            home / Path(*target.parts),
            None,
        ),
        "source": None,
        "owner": owner,
        "link_target": None,
        "release_sha": None,
        "before_evidence": None,
        "before_evidence_identity": None,
        "backup": None,
        "stage": None,
        "stage_identity": None,
        "evidence": None,
        "evidence_identity": None,
    }


def _projected_pending_claim_payload(
    phase: str,
    semantic: tuple[
        str,
        PurePosixPath,
        str,
        PurePosixPath | None,
        str,
        str,
        str,
    ],
    index: int,
) -> dict[str, Any]:
    scope, target, kind, source, owner, link_target, release_sha = semantic
    return {
        "index": index,
        "scope": scope,
        "target": target.as_posix(),
        "kind": kind,
        "source": source.as_posix() if source is not None else None,
        "owner": owner,
        "link_target": link_target,
        "release_sha": release_sha,
        "parent_identity": _identity_payload(_MAX_PENDING_IDENTITY),
        "link_identity": _identity_payload(_MAX_PENDING_IDENTITY),
        "evidence": PurePosixPath(
            "pending",
            "claims",
            phase,
            f"{index:08d}",
        ).as_posix(),
    }


def _projected_pending_claim_payloads(
    home: Path,
    phase: str,
    state: ManagedState,
    record_actions: dict[tuple[str, PurePosixPath], str],
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for semantic in _pending_state_claim_semantics(home, state):
        action = record_actions.get((semantic[0], semantic[1]))
        if phase == "before" and action in {"create", "retire-absent"}:
            continue
        claims.append(
            _projected_pending_claim_payload(
                phase,
                semantic,
                len(claims),
            )
        )
    return claims


def _projected_pending_release_payloads(
    state: ManagedState,
) -> list[dict[str, Any]]:
    return [
        {
            "owner": owner,
            "sha": sha,
            "directory_identity": _identity_payload(_MAX_PENDING_IDENTITY),
            "tree_sha256": _MAX_PENDING_DIGEST,
        }
        for owner, sha in sorted(state.owners.items())
    ]


def _projected_pending_metadata_payload(
    *,
    state_before_exists: bool,
    records: list[dict[str, Any]],
    claims_before: list[dict[str, Any]],
    claims_after: list[dict[str, Any]],
    releases_before: list[dict[str, Any]],
    releases_after: list[dict[str, Any]],
) -> dict[str, Any]:
    state_before_evidence = (
        PENDING_STATE_BEFORE_EVIDENCE.as_posix()
        if state_before_exists
        else None
    )
    return {
        "version": 4,
        "batch": _MAX_PENDING_BATCH_NAME,
        "state_parent_identity": _identity_payload(_MAX_PENDING_IDENTITY),
        "state_before": {
            "exists": state_before_exists,
            "mode": 0o7777 if state_before_exists else None,
            "sha256": _MAX_PENDING_DIGEST if state_before_exists else None,
            "identity": (
                _identity_payload(_MAX_PENDING_IDENTITY)
                if state_before_exists
                else None
            ),
            "evidence": state_before_evidence,
        },
        "state_after": {
            "exists": True,
            "mode": 0o600,
            "sha256": _MAX_PENDING_DIGEST,
            "identity": _identity_payload(_MAX_PENDING_IDENTITY),
            "evidence": PENDING_STATE_AFTER_EVIDENCE.as_posix(),
        },
        "releases_before": releases_before,
        "releases_after": releases_after,
        "commit_evidence": {
            "exists": True,
            "mode": 0o600,
            "sha256": _MAX_PENDING_DIGEST,
            "identity": _identity_payload(_MAX_PENDING_IDENTITY),
            "evidence": PENDING_STATE_COMMIT_EVIDENCE.as_posix(),
        },
        "commit_marker": PENDING_STATE_COMMIT_MARKER.as_posix(),
        "claims_before": claims_before,
        "claims_after": claims_after,
        "records": records,
    }


def _projected_json_size(payload: object, *, trailing_newline: bool) -> int:
    size = 0
    encoder = json.JSONEncoder(indent=2, sort_keys=False)
    try:
        for chunk in encoder.iterencode(payload):
            size += len(chunk.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as error:
        raise SyncError(f"failed to size projected JSON document: {error}") from error
    return size + int(trailing_newline)


def _projected_top_level_array_element_size(payload: dict[str, Any]) -> int:
    try:
        encoded = json.dumps(payload, indent=2, sort_keys=False)
        encoded_size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as error:
        raise SyncError(f"failed to size projected JSON array item: {error}") from error
    return encoded_size + 4 * (encoded.count("\n") + 1)


def _projected_index_digit_delta(count: int) -> int:
    if count < 0:
        raise SyncError("projected JSON array item count is negative")
    delta = 0
    lower = 10
    digits = 2
    while lower < count:
        upper = min(count, lower * 10)
        delta += (upper - lower) * (digits - 1)
        lower *= 10
        digits += 1
    return delta


def _projected_top_level_array_delta(
    element_size_sum: int,
    count: int,
    *,
    indexed: bool,
) -> int:
    if count == 0:
        return 0
    if count < 0 or element_size_sum < 0:
        raise SyncError("projected JSON array size is negative")
    return (
        element_size_sum
        + 2 * count
        + 2
        + (_projected_index_digit_delta(count) if indexed else 0)
    )


def _manifest_transition_capacity_profile(
    owner: str,
    links: dict[str, dict[str, Any]],
    removed_links: dict[str, dict[str, Any]],
) -> ManifestTransitionCapacityProfile:
    if OWNER_RE.fullmatch(owner) is None:
        raise SyncError(f"manifest transition owner is invalid: {owner}")
    home = Path("/home/codex/.codex")
    release_sha = "f" * 40
    state_links: dict[PurePosixPath, ManagedLinkRecord] = {}
    for target_text, raw_link in links.items():
        try:
            source = PurePosixPath(raw_link["source"])
            target = PurePosixPath(raw_link["target"])
            kind = raw_link["kind"]
        except (KeyError, TypeError, ValueError) as error:
            raise SyncError("manifest transition link is invalid") from error
        if target.as_posix() != target_text:
            raise SyncError(f"manifest transition target key changed: {target_text}")
        entry = LinkEntry(
            source=source,
            target=target,
            kind=kind,
            owner=owner,
        )
        state_links[target] = ManagedLinkRecord(
            source=source,
            target=target,
            kind=kind,
            owner=owner,
            link_target=_desired_link_target(home, entry),
            release_sha=release_sha,
        )
    state = ManagedState(
        owners={owner: release_sha},
        links=state_links,
    )
    managed_state_size = len(_managed_state_bytes(state))
    if managed_state_size > MAX_MANAGED_STATE_BYTES:
        raise SyncError("planned managed state exceeds the size limit")

    before_current_claim_size: int | None = None
    before_claim_sizes: dict[PurePosixPath, int] = {}
    for semantic in _pending_state_claim_semantics(home, state):
        item_size = _projected_top_level_array_element_size(
            _projected_pending_claim_payload("before", semantic, 0)
        )
        if semantic[0] == "current":
            if before_current_claim_size is not None:
                raise SyncError("manifest transition has multiple current claims")
            before_current_claim_size = item_size
        else:
            before_claim_sizes[semantic[1]] = item_size
    if before_current_claim_size is None:
        raise SyncError("manifest transition is missing its current claim")

    after_claim_size_sum = 0
    after_claim_count = 0
    for semantic in _pending_state_claim_semantics(home, state):
        after_claim_size_sum += _projected_top_level_array_element_size(
            _projected_pending_claim_payload("after", semantic, 0)
        )
        after_claim_count += 1

    release_payloads = _projected_pending_release_payloads(state)
    release_size_sum = sum(
        _projected_top_level_array_element_size(payload)
        for payload in release_payloads
    )
    current_action = ReconcileAction(
        "replace",
        _current_link(home, owner),
        f"releases/{release_sha}",
        "directory",
        expected_link_target=_MAX_PENDING_LINK_TARGET,
    )
    current_record_size = _projected_top_level_array_element_size(
        _projected_pending_record_payload(
            home,
            "current",
            current_action,
            state,
            state,
            state,
            0,
        )
    )

    create_record_sizes: dict[PurePosixPath, int] = {}
    remove_record_sizes: dict[PurePosixPath, int] = {}
    retired_absence_record_sizes: dict[PurePosixPath, int] = {}
    for target, record in state.links.items():
        target_path = home / Path(*target.parts)
        create_record_sizes[target] = _projected_top_level_array_element_size(
            _projected_pending_record_payload(
                home,
                "managed",
                ReconcileAction(
                    "create",
                    target_path,
                    record.link_target,
                    record.kind,
                    planned_snapshot=ReconcileTargetSnapshot(
                        parent_identity=_MAX_PENDING_IDENTITY,
                    ),
                ),
                state,
                state,
                state,
                0,
            )
        )
        remove_record_sizes[target] = _projected_top_level_array_element_size(
            _projected_pending_record_payload(
                home,
                "managed",
                ReconcileAction(
                    "remove",
                    target_path,
                    "",
                    record.kind,
                    expected_link_target=record.link_target,
                    planned_snapshot=ReconcileTargetSnapshot(
                        parent_identity=_MAX_PENDING_IDENTITY,
                        link_identity=_MAX_PENDING_IDENTITY,
                        link_target=record.link_target,
                        ancestor_identity=_MAX_PENDING_IDENTITY,
                    ),
                ),
                state,
                state,
                state,
                0,
            )
        )
        retired_absence_record_sizes[target] = (
            _projected_top_level_array_element_size(
                _projected_retired_absence_record_payload(
                    home,
                    target,
                    record,
                    0,
                )
            )
        )
    historical_record_sizes: dict[PurePosixPath, int] = {}
    for raw_removed in removed_links.values():
        try:
            source = PurePosixPath(raw_removed["source"])
            target = PurePosixPath(raw_removed["target"])
            kind = raw_removed["kind"]
        except (KeyError, TypeError, ValueError) as error:
            raise SyncError("manifest transition removed link is invalid") from error
        removed_target = _desired_link_target(
            home,
            LinkEntry(
                source=source,
                target=target,
                kind=kind,
                owner=owner,
            ),
        )
        target_path = home / Path(*target.parts)
        current_record = state.links.get(target)
        if current_record is None:
            action = ReconcileAction(
                "quarantine-remove",
                target_path,
                "",
                kind,
                expected_link_target=removed_target,
                planned_snapshot=ReconcileTargetSnapshot(
                    parent_identity=_MAX_PENDING_IDENTITY,
                    link_identity=_MAX_PENDING_IDENTITY,
                    link_target=removed_target,
                    ancestor_identity=_MAX_PENDING_IDENTITY,
                ),
            )
        else:
            action = ReconcileAction(
                "quarantine-replace",
                target_path,
                current_record.link_target,
                current_record.kind,
                expected_link_target=removed_target,
                planned_snapshot=ReconcileTargetSnapshot(
                    parent_identity=_MAX_PENDING_IDENTITY,
                    link_identity=_MAX_PENDING_IDENTITY,
                    link_target=removed_target,
                    ancestor_identity=_MAX_PENDING_IDENTITY,
                ),
            )
        record_size = _projected_top_level_array_element_size(
            _projected_pending_record_payload(
                home,
                "managed",
                action,
                state,
                state,
                state,
                0,
            )
        )
        historical_record_sizes[target] = max(
            historical_record_sizes.get(target, 0),
            record_size,
        )
    return ManifestTransitionCapacityProfile(
        owner=owner,
        state=state,
        managed_state_size=managed_state_size,
        before_current_claim_size=before_current_claim_size,
        before_claim_sizes=before_claim_sizes,
        after_claim_size_sum=after_claim_size_sum,
        after_claim_count=after_claim_count,
        release_size_sum=release_size_sum,
        release_count=len(release_payloads),
        current_record_size=current_record_size,
        create_record_sizes=create_record_sizes,
        remove_record_sizes=remove_record_sizes,
        retired_absence_record_sizes=retired_absence_record_sizes,
        historical_record_sizes=historical_record_sizes,
    )


def _manifest_transition_metadata_size(
    previous: ManifestTransitionCapacityProfile,
    current: ManifestTransitionCapacityProfile,
) -> int:
    if previous.owner != current.owner:
        raise SyncError("manifest transition owner changed")
    home = Path("/home/codex/.codex")
    before_record_element_sum = (
        previous.before_current_claim_size + current.current_record_size
    )
    before_count_max = 1
    record_count_max = 1
    total_before_record_count_max = 2
    targets = sorted(
        set(previous.state.links)
        | set(current.state.links)
        | set(current.historical_record_sizes),
        key=PurePosixPath.as_posix,
    )
    for target in targets:
        previous_record = previous.state.links.get(target)
        current_record = current.state.links.get(target)
        historical_record_size = current.historical_record_sizes.get(target)
        if previous_record == current_record:
            if previous_record is None:
                if historical_record_size is None:
                    raise SyncError(
                        f"manifest transition target has no projected action: {target}"
                    )
                before_record_element_sum += historical_record_size
                record_count_max += 1
                total_before_record_count_max += 1
                continue
            before_record_element_sum += max(
                previous.before_claim_sizes[target],
                current.create_record_sizes[target],
                historical_record_size or 0,
            )
            before_count_max += 1
            record_count_max += 1
            total_before_record_count_max += 1
            continue
        if previous_record is None:
            assert current_record is not None
            before_record_element_sum += max(
                current.create_record_sizes[target],
                historical_record_size or 0,
            )
            record_count_max += 1
            total_before_record_count_max += 1
            continue
        before_claim_size = previous.before_claim_sizes[target]
        if current_record is None:
            canonical_size = (
                before_claim_size + previous.remove_record_sizes[target]
            )
            noncanonical_size = max(
                previous.retired_absence_record_sizes[target],
                historical_record_size or 0,
            )
        else:
            target_path = home / Path(*target.parts)
            replace_payload = _projected_pending_record_payload(
                home,
                "managed",
                ReconcileAction(
                    "replace",
                    target_path,
                    current_record.link_target,
                    current_record.kind,
                    expected_link_target=previous_record.link_target,
                    planned_snapshot=ReconcileTargetSnapshot(
                        parent_identity=_MAX_PENDING_IDENTITY,
                        link_identity=_MAX_PENDING_IDENTITY,
                        link_target=previous_record.link_target,
                        ancestor_identity=_MAX_PENDING_IDENTITY,
                    ),
                ),
                previous.state,
                previous.state,
                current.state,
                0,
            )
            canonical_size = (
                before_claim_size
                + _projected_top_level_array_element_size(replace_payload)
            )
            noncanonical_size = max(
                current.create_record_sizes[target],
                historical_record_size or 0,
            )
        before_record_element_sum += max(
            canonical_size,
            noncanonical_size,
        )
        before_count_max += 1
        record_count_max += 1
        total_before_record_count_max += 2

    if record_count_max > MAX_PENDING_LINK_RECORDS:
        raise SyncError("projected pending transaction has too many records")
    if before_count_max > MAX_PENDING_LINK_CLAIMS:
        raise SyncError("projected pending transaction has too many before-state claims")
    if current.after_claim_count > MAX_PENDING_LINK_CLAIMS:
        raise SyncError("projected pending transaction has too many after-state claims")
    before_record_delta = (
        before_record_element_sum
        + 2 * total_before_record_count_max
        + 4
        + _projected_index_digit_delta(before_count_max)
        + _projected_index_digit_delta(record_count_max)
    )
    envelope = _projected_pending_metadata_payload(
        state_before_exists=True,
        records=[],
        claims_before=[],
        claims_after=[],
        releases_before=[],
        releases_after=[],
    )
    return (
        _projected_json_size(envelope, trailing_newline=True)
        + before_record_delta
        + _projected_top_level_array_delta(
            current.after_claim_size_sum,
            current.after_claim_count,
            indexed=True,
        )
        + _projected_top_level_array_delta(
            previous.release_size_sum,
            previous.release_count,
            indexed=False,
        )
        + _projected_top_level_array_delta(
            current.release_size_sum,
            current.release_count,
            indexed=False,
        )
    )


def _validate_manifest_transition_capacity(
    previous: ManifestTransitionCapacityProfile,
    current: ManifestTransitionCapacityProfile,
) -> None:
    if current.managed_state_size > MAX_MANAGED_STATE_BYTES:
        raise SyncError("planned managed state exceeds the size limit")
    projected_size = _manifest_transition_metadata_size(previous, current)
    if projected_size > MAX_MANAGED_STATE_BYTES:
        raise SyncError(
            "pending link transaction metadata exceeds the size limit: "
            f"{projected_size} > {MAX_MANAGED_STATE_BYTES}"
        )


def _validate_pending_link_metadata_capacity(
    home: Path,
    capacity: PendingLinkCapacityPlan,
    state_before: ManagedStateFileSnapshot,
    state_before_value: ManagedState,
    planning_state_before: ManagedState,
    state_after_value: ManagedState,
) -> None:
    records: list[dict[str, Any]] = []
    record_actions: dict[tuple[str, PurePosixPath], str] = {}
    for scope, actions in capacity.ordered_groups:
        for action in actions:
            target = PurePosixPath(*action.target.relative_to(home).parts)
            record_actions[(scope, target)] = action.action
            records.append(
                _projected_pending_record_payload(
                    home,
                    scope,
                    action,
                    state_before_value,
                    planning_state_before,
                    state_after_value,
                    len(records),
                )
            )
    for target, state_record in capacity.retired_absence_specs:
        record_actions[("managed", target)] = "retire-absent"
        records.append(
            _projected_retired_absence_record_payload(
                home,
                target,
                state_record,
                len(records),
            )
        )
    for target, owner in capacity.retired_current_absence_specs:
        record_actions[("current", target)] = "retire-absent"
        records.append(
            _projected_retired_current_absence_record_payload(
                home,
                target,
                owner,
                len(records),
            )
        )
    payload = _projected_pending_metadata_payload(
        state_before_exists=state_before.exists,
        records=records,
        claims_before=_projected_pending_claim_payloads(
            home,
            "before",
            state_before_value,
            record_actions,
        ),
        claims_after=_projected_pending_claim_payloads(
            home,
            "after",
            state_after_value,
            record_actions,
        ),
        releases_before=_projected_pending_release_payloads(
            state_before_value
        ),
        releases_after=_projected_pending_release_payloads(
            state_after_value
        ),
    )
    _bounded_json_document(
        payload,
        max_bytes=MAX_MANAGED_STATE_BYTES,
        overflow_error="pending link transaction metadata exceeds the size limit",
    )


def _build_pending_link_capacity_plan(
    home: Path,
    action_groups: list[tuple[str, list[ReconcileAction]]],
    state_before: ManagedStateFileSnapshot,
    state_before_value: ManagedState,
    planning_state_before: ManagedState,
    state_after_value: ManagedState,
    *,
    required_replacements_by_scope: (
        dict[str, dict[Path, list[LinkEntry]]] | None
    ) = None,
) -> PendingLinkCapacityPlan:
    ordered_groups = tuple(
        (
            scope,
            tuple(
                _ordered_reconcile_actions(
                    home,
                    actions,
                    (required_replacements_by_scope or {}).get(scope),
                )
            ),
        )
        for scope, actions in action_groups
    )
    flattened_actions = tuple(
        action for _scope, actions in ordered_groups for action in actions
    )
    if len(flattened_actions) > MAX_PENDING_LINK_RECORDS:
        raise SyncError("pending transaction has too many actions")
    action_keys: set[tuple[str, PurePosixPath]] = set()
    create_keys: set[tuple[str, PurePosixPath]] = set()
    for scope, actions in ordered_groups:
        if scope not in {"current", "managed"}:
            raise SyncError(f"unsupported pending transaction scope: {scope}")
        for action in actions:
            try:
                relative_target = action.target.relative_to(home)
            except ValueError as error:
                raise SyncError(
                    f"pending target is outside home: {action.target}"
                ) from error
            key = (scope, PurePosixPath(*relative_target.parts))
            if key in action_keys:
                raise SyncError(f"duplicate pending transaction target: {action.target}")
            action_keys.add(key)
            if action.action == "create":
                create_keys.add(key)
    retired_absence_specs: list[tuple[PurePosixPath, ManagedLinkRecord]] = []
    for target in sorted(
        set(state_before_value.links) - set(state_after_value.links),
        key=PurePosixPath.as_posix,
    ):
        if ("managed", target) in action_keys:
            continue
        retired_absence_specs.append(
            (
                target,
                state_before_value.links[target],
            )
        )
    retired_current_absence_specs: list[tuple[PurePosixPath, str]] = []
    for owner in sorted(set(state_before_value.owners) - set(state_after_value.owners)):
        current_target = PurePosixPath(
            *_current_link(home, owner).relative_to(home).parts
        )
        if ("current", current_target) in action_keys:
            continue
        current_snapshot = _capture_reconcile_target_snapshot(
            home,
            home / Path(*current_target.parts),
        )
        if (
            current_snapshot.link_identity is not None
            or current_snapshot.link_target is not None
        ):
            raise SyncError(
                f"pending current retirement has no removal action: {current_target}"
            )
        retired_current_absence_specs.append((current_target, owner))
    if (
        len(flattened_actions)
        + len(retired_absence_specs)
        + len(retired_current_absence_specs)
        > MAX_PENDING_LINK_RECORDS
    ):
        raise SyncError("pending transaction has too many records")
    retired_absence_keys = {
        ("managed", target) for target, _record in retired_absence_specs
    }
    retired_absence_keys.update(
        ("current", target)
        for target, _owner in retired_current_absence_specs
    )
    omitted_before_keys = create_keys | retired_absence_keys
    before_claim_count = sum(
        (semantic[0], semantic[1]) not in omitted_before_keys
        for semantic in _pending_state_claim_semantics(
            home,
            state_before_value,
        )
    )
    after_claim_count = len(
        _pending_state_claim_semantics(home, state_after_value)
    )
    if before_claim_count > MAX_PENDING_LINK_CLAIMS:
        raise SyncError("pending transaction has too many before-state claims")
    if after_claim_count > MAX_PENDING_LINK_CLAIMS:
        raise SyncError("pending transaction has too many after-state claims")
    if len(state_before_value.owners) > MAX_PENDING_RELEASES:
        raise SyncError(
            "pending transaction has too many before-state release expectations"
        )
    if len(state_after_value.owners) > MAX_PENDING_RELEASES:
        raise SyncError(
            "pending transaction has too many after-state release expectations"
        )
    if len(_managed_state_bytes(state_after_value)) > MAX_MANAGED_STATE_BYTES:
        raise SyncError("planned managed state exceeds the size limit")
    capacity = PendingLinkCapacityPlan(
        ordered_groups=ordered_groups,
        flattened_actions=flattened_actions,
        retired_absence_specs=tuple(retired_absence_specs),
        retired_current_absence_specs=tuple(retired_current_absence_specs),
    )
    _validate_pending_link_metadata_capacity(
        home,
        capacity,
        state_before,
        state_before_value,
        planning_state_before,
        state_after_value,
    )
    return capacity


def _build_pending_link_batch_plan(
    home: Path,
    action_groups: list[tuple[str, list[ReconcileAction]]],
    state_before: ManagedStateFileSnapshot,
    planning_state_before: ManagedState,
    state_after_value: ManagedState,
    *,
    required_replacements_by_scope: (
        dict[str, dict[Path, list[LinkEntry]]] | None
    ) = None,
) -> PendingLinkBatchPlan:
    if not _canonical_managed_state_matches_snapshot(home, state_before):
        raise SyncError("managed state changed before pending transaction staging")
    canonical_state_before_value = _managed_state_value_from_snapshot(
        home,
        state_before,
    )
    capacity = _build_pending_link_capacity_plan(
        home,
        action_groups,
        state_before,
        canonical_state_before_value,
        planning_state_before,
        state_after_value,
        required_replacements_by_scope=required_replacements_by_scope,
    )
    for _scope, actions in capacity.ordered_groups:
        for action in actions:
            if action.action != "create":
                continue
            snapshot = action.planned_snapshot
            if (
                snapshot is None
                or snapshot.link_identity is not None
                or snapshot.link_target is not None
            ):
                raise SyncError(
                    f"pending create target was not absent when planned: {action.target}"
                )
    return PendingLinkBatchPlan(
        capacity=capacity,
        state_before_value=canonical_state_before_value,
    )


def _stage_pending_link_batch(
    home: Path,
    action_groups: list[tuple[str, list[ReconcileAction]]],
    desired_entries: list[LinkEntry],
    owner_shas: dict[str, str],
    state_before: ManagedStateFileSnapshot,
    planning_state_before: ManagedState,
    state_after_value: ManagedState,
    *,
    required_replacements_by_scope: (
        dict[str, dict[Path, list[LinkEntry]]] | None
    ) = None,
) -> PendingLinkBatch:
    plan = _build_pending_link_batch_plan(
        home,
        action_groups,
        state_before,
        planning_state_before,
        state_after_value,
        required_replacements_by_scope=required_replacements_by_scope,
    )
    desired_by_target = _entries_by_target(desired_entries)
    ordered_groups = plan.capacity.ordered_groups
    flattened_actions = list(plan.capacity.flattened_actions)
    canonical_state_before_value = plan.state_before_value
    retired_absence_specs = plan.capacity.retired_absence_specs
    retired_current_absence_specs = plan.capacity.retired_current_absence_specs
    batch_root = _quarantine_batch_root(home, flattened_actions)
    batch_root_fd = _open_directory_beneath(home, batch_root)
    try:
        batch_root_identity = _directory_identity(batch_root_fd)
        if not _bound_directory_matches(home, batch_root, batch_root_fd):
            raise SyncError(f"quarantine batch changed: {batch_root}")
    finally:
        _close_fd_quietly(batch_root_fd)
    directories = [
        batch_root / "pending" / "before",
        batch_root / "pending" / "stage",
        batch_root / "pending" / "evidence",
        batch_root / "pending" / "state",
        batch_root / "pending" / "claims" / "before",
        batch_root / "pending" / "claims" / "after",
    ]
    for directory in directories:
        directory_fd = _open_or_create_directory_beneath(home, directory, mode=0o700)
        _close_fd_quietly(directory_fd)
    records: list[PendingLinkRecord] = []
    seen: set[tuple[str, PurePosixPath]] = set()
    created_parent_identities: dict[Path, tuple[int, int]] = {}
    try:
        for scope, actions in ordered_groups:
            for action in actions:
                planned_snapshot = action.planned_snapshot
                if (
                    planned_snapshot is not None
                    and planned_snapshot.parent_identity is None
                ):
                    if action.action != "create":
                        raise SyncError(
                            "only a create action may have a missing parent during "
                            f"pending staging: {action.target}"
                        )
                    parent_fd = _open_reconcile_parent_for_create(
                        home,
                        action.target,
                        planned_snapshot,
                        created_parent_identities,
                    )
                    _close_fd_quietly(parent_fd)
                    refreshed_snapshot = _capture_reconcile_target_snapshot(
                        home,
                        action.target,
                    )
                    if (
                        refreshed_snapshot.parent_identity is None
                        or refreshed_snapshot.link_identity is not None
                        or refreshed_snapshot.link_target is not None
                        or refreshed_snapshot.missing_parent_parts
                    ):
                        raise SyncError(
                            "pending target parent could not be durably bound: "
                            f"{action.target.parent}"
                        )
                    action = replace(
                        action,
                        planned_snapshot=refreshed_snapshot,
                    )
                record = _pending_link_record_for_action(
                    home,
                    scope,
                    action,
                    desired_by_target,
                    owner_shas,
                    planning_state_before,
                    len(records),
                )
                if record.action == "create":
                    _require_pending_record_bound_create_absence(record)
                key = (record.scope, record.target)
                if key in seen:
                    raise SyncError(f"duplicate pending transaction target: {record.target}")
                seen.add(key)
                if record.before_evidence is not None:
                    assert record.planned_snapshot.link_identity is not None
                    assert record.planned_snapshot.link_target is not None
                    before_path = batch_root / Path(*record.before_evidence.parts)
                    before_plan = _capture_reconcile_target_snapshot(home, before_path)
                    before_snapshot = _publish_symlink_hardlink_beneath(
                        home,
                        action.target,
                        before_path,
                        SymlinkSnapshot(
                            parent_identity=record.planned_snapshot.parent_identity,
                            link_identity=record.planned_snapshot.link_identity,
                            link_target=record.planned_snapshot.link_target,
                        ),
                        before_plan,
                        created_parent_identities,
                    )
                    if before_snapshot.link_identity != record.before_evidence_identity:
                        raise SyncError(f"pending before evidence changed: {record.target}")
                if record.stage is not None:
                    assert record.evidence is not None
                    assert record.link_target is not None
                    stage = batch_root / Path(*record.stage.parts)
                    evidence = batch_root / Path(*record.evidence.parts)
                    stage_snapshot = _create_symlink_beneath(
                        home,
                        stage,
                        record.link_target,
                        record.kind,
                    )
                    evidence_plan = _capture_reconcile_target_snapshot(home, evidence)
                    evidence_snapshot = _publish_symlink_hardlink_beneath(
                        home,
                        stage,
                        evidence,
                        stage_snapshot,
                        evidence_plan,
                        created_parent_identities,
                    )
                    record = replace(
                        record,
                        stage_identity=stage_snapshot.link_identity,
                        evidence_identity=evidence_snapshot.link_identity,
                    )
                records.append(record)

        for target, state_record in retired_absence_specs:
            planned_snapshot = _capture_reconcile_target_snapshot(
                home,
                home / Path(*target.parts),
            )
            if (
                planned_snapshot.link_identity is not None
                or planned_snapshot.link_target is not None
            ):
                raise SyncError(
                    f"retired managed target changed before pending staging: {target}"
                )
            record = PendingLinkRecord(
                index=len(records),
                scope="managed",
                action="retire-absent",
                target=target,
                kind=state_record.kind,
                planned_snapshot=planned_snapshot,
                source=None,
                owner=None,
                link_target=None,
                release_sha=None,
                before_evidence=None,
                before_evidence_identity=None,
                backup=None,
                stage=None,
                stage_identity=None,
                evidence=None,
                evidence_identity=None,
            )
            _require_pending_record_bound_retired_absence(record)
            key = (record.scope, record.target)
            if key in seen:
                raise SyncError(
                    f"duplicate pending transaction target: {record.target}"
                )
            seen.add(key)
            records.append(record)

        for target, owner in retired_current_absence_specs:
            planned_snapshot = _capture_reconcile_target_snapshot(
                home,
                home / Path(*target.parts),
            )
            if (
                planned_snapshot.link_identity is not None
                or planned_snapshot.link_target is not None
            ):
                raise SyncError(
                    f"retired current target changed before pending staging: {target}"
                )
            record = PendingLinkRecord(
                index=len(records),
                scope="current",
                action="retire-absent",
                target=target,
                kind="directory",
                planned_snapshot=planned_snapshot,
                source=None,
                owner=owner,
                link_target=None,
                release_sha=None,
                before_evidence=None,
                before_evidence_identity=None,
                backup=None,
                stage=None,
                stage_identity=None,
                evidence=None,
                evidence_identity=None,
            )
            _require_pending_record_bound_retired_absence(record)
            key = (record.scope, record.target)
            if key in seen:
                raise SyncError(
                    f"duplicate pending transaction target: {record.target}"
                )
            seen.add(key)
            records.append(record)

        record_tuple = tuple(records)
        claims_before = _stage_pending_link_claims(
            home,
            batch_root,
            "before",
            canonical_state_before_value,
            record_tuple,
            created_parent_identities,
        )
        claims_after = _stage_pending_link_claims(
            home,
            batch_root,
            "after",
            state_after_value,
            record_tuple,
            created_parent_identities,
        )
        state_before_evidence: PurePosixPath | None = None
        if state_before.exists:
            state_before_evidence = PENDING_STATE_BEFORE_EVIDENCE
            _publish_regular_hardlink_beneath(
                home,
                _state_path(home),
                batch_root / Path(*state_before_evidence.parts),
                state_before,
            )
        after_payload = _managed_state_bytes(state_after_value)
        if len(after_payload) > MAX_MANAGED_STATE_BYTES:
            raise SyncError("planned managed state exceeds the size limit")
        raw_state_after = _write_exclusive_internal_file(
            home,
            batch_root / Path(*PENDING_STATE_AFTER_EVIDENCE.parts),
            after_payload,
        )
        state_after = ManagedStateFileSnapshot(
            exists=True,
            payload=raw_state_after.payload,
            mode=raw_state_after.mode,
            parent_identity=state_before.parent_identity,
            file_identity=raw_state_after.file_identity,
        )
        release_expectation_cache: dict[
            tuple[str, str], PendingReleaseExpectation
        ] = {}
        releases_before = _pending_release_expectations_for_state(
            home,
            canonical_state_before_value,
            release_expectation_cache,
        )
        releases_after = _pending_release_expectations_for_state(
            home,
            state_after_value,
            release_expectation_cache,
        )
        commit_evidence = _write_exclusive_internal_file(
            home,
            batch_root / Path(*PENDING_STATE_COMMIT_EVIDENCE.parts),
            _pending_commit_evidence_payload(batch_root, state_after),
        )
        for directory in (*directories, batch_root):
            directory_fd = _open_directory_beneath(home, directory)
            try:
                os.fsync(directory_fd)
            finally:
                _close_fd_quietly(directory_fd)
        metadata_path = batch_root / PENDING_LINK_METADATA_NAME
        _write_exclusive_internal_file(
            home,
            metadata_path,
            _pending_link_metadata_payload(
                batch_root,
                record_tuple,
                claims_before,
                claims_after,
                state_before,
                state_after,
                state_before_evidence,
                PENDING_STATE_AFTER_EVIDENCE,
                releases_before,
                releases_after,
                commit_evidence,
            ),
        )
    except BaseException:
        # The unreferenced durable batch is deliberately retained for auditability.
        raise
    return PendingLinkBatch(
        batch_root=batch_root,
        batch_root_identity=batch_root_identity,
        records=record_tuple,
        claims_before=claims_before,
        claims_after=claims_after,
        state_before=state_before,
        state_after=state_after,
        state_before_evidence=state_before_evidence,
        state_after_evidence=PENDING_STATE_AFTER_EVIDENCE,
        state_before_value=canonical_state_before_value,
        state_after_value=state_after_value,
        releases_before=releases_before,
        releases_after=releases_after,
        commit_evidence=commit_evidence,
        commit_evidence_path=PENDING_STATE_COMMIT_EVIDENCE,
        commit_marker_path=PENDING_STATE_COMMIT_MARKER,
    )


def _parse_pending_identity(value: object, label: str) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(
            not isinstance(part, int) or isinstance(part, bool) or part < 0 or part >= 2**64
            for part in value
        )
    ):
        raise SyncError(f"{label} must be a bounded device/inode pair")
    return value[0], value[1]


def _parse_pending_planned_snapshot(value: object) -> ReconcileTargetSnapshot:
    if not isinstance(value, dict) or set(value) != {
        "parent_identity",
        "link_identity",
        "link_target",
        "ancestor_identity",
        "missing_parent_parts",
    }:
        raise SyncError("pending transaction planned-before snapshot is invalid")
    link_target = value.get("link_target")
    if link_target is not None and (not isinstance(link_target, str) or not link_target):
        raise SyncError("pending transaction planned link target is invalid")
    raw_missing = value.get("missing_parent_parts")
    if (
        not isinstance(raw_missing, list)
        or len(raw_missing) > MAX_ARCHIVE_MEMBER_PATH_DEPTH
        or any(
            not isinstance(part, str) or part in {"", ".", ".."} or "/" in part
            for part in raw_missing
        )
    ):
        raise SyncError("pending transaction missing-parent binding is invalid")
    try:
        return ReconcileTargetSnapshot(
            parent_identity=_parse_pending_identity(
                value.get("parent_identity"),
                "pending parent identity",
            ),
            link_identity=_parse_pending_identity(
                value.get("link_identity"),
                "pending link identity",
            ),
            link_target=link_target,
            ancestor_identity=_parse_pending_identity(
                value.get("ancestor_identity"),
                "pending ancestor identity",
            ),
            missing_parent_parts=tuple(raw_missing),
        )
    except ValueError as error:
        raise SyncError(f"pending transaction planned-before snapshot is invalid: {error}") from error


def _parse_pending_relative_or_none(value: object, label: str) -> PurePosixPath | None:
    if value is None:
        return None
    return _validate_relative_path(value, label)


def _parse_pending_link_claims(
    home: Path,
    batch_root: Path,
    phase: str,
    raw_claims: object,
    state: ManagedState,
    *,
    omitted_keys: set[tuple[str, PurePosixPath]] | None = None,
) -> tuple[PendingLinkClaim, ...]:
    if phase not in {"before", "after"}:
        raise SyncError(f"unsupported pending claim phase: {phase}")
    omitted_keys = set() if omitted_keys is None else omitted_keys
    if phase != "before" and omitted_keys:
        raise SyncError("only pending before-state claims may use absence coverage")
    all_semantics = _pending_state_claim_semantics(home, state)
    semantic_keys = {(semantic[0], semantic[1]) for semantic in all_semantics}
    if not omitted_keys.issubset(semantic_keys):
        raise SyncError(
            f"pending transaction {phase} absence coverage is not state-claimed"
        )
    expected_semantics = [
        semantic
        for semantic in all_semantics
        if (semantic[0], semantic[1]) not in omitted_keys
    ]
    if (
        not isinstance(raw_claims, list)
        or len(raw_claims) > MAX_PENDING_LINK_CLAIMS
        or len(raw_claims) != len(expected_semantics)
    ):
        raise SyncError(
            f"pending transaction {phase} claims do not exactly match state"
        )
    expected_fields = {
        "index",
        "scope",
        "target",
        "kind",
        "source",
        "owner",
        "link_target",
        "release_sha",
        "parent_identity",
        "link_identity",
        "evidence",
    }
    claims: list[PendingLinkClaim] = []
    for index, (raw_claim, expected) in enumerate(
        zip(raw_claims, expected_semantics)
    ):
        if not isinstance(raw_claim, dict) or set(raw_claim) != expected_fields:
            raise SyncError(f"pending transaction {phase} claim #{index + 1} is invalid")
        if raw_claim.get("index") != index:
            raise SyncError(f"pending transaction {phase} claim order changed")
        scope = raw_claim.get("scope")
        if scope not in {"current", "managed"}:
            raise SyncError(f"pending transaction {phase} claim scope is invalid")
        target = _validate_relative_path(
            raw_claim.get("target"),
            f"pending {phase} claim target",
        )
        kind = raw_claim.get("kind")
        if kind not in {"file", "directory", "skill"}:
            raise SyncError(f"pending {phase} claim kind is invalid: {target}")
        source = _parse_pending_relative_or_none(
            raw_claim.get("source"),
            f"pending {phase} claim source",
        )
        owner = _validate_owner(
            raw_claim.get("owner"),
            f"pending {phase} claim owner",
        )
        link_target = raw_claim.get("link_target")
        if not isinstance(link_target, str) or not link_target:
            raise SyncError(f"pending {phase} claim link target is invalid: {target}")
        release_sha = _validate_release_sha(raw_claim.get("release_sha"))
        parent_identity = _parse_pending_identity(
            raw_claim.get("parent_identity"),
            f"pending {phase} claim parent identity",
        )
        link_identity = _parse_pending_identity(
            raw_claim.get("link_identity"),
            f"pending {phase} claim link identity",
        )
        evidence = _parse_pending_relative_or_none(
            raw_claim.get("evidence"),
            f"pending {phase} claim evidence",
        )
        expected_evidence = PurePosixPath(
            "pending",
            "claims",
            phase,
            f"{index:08d}",
        )
        semantic = (
            scope,
            target,
            kind,
            source,
            owner,
            link_target,
            release_sha,
        )
        if semantic != expected:
            raise SyncError(
                f"pending transaction {phase} claim does not match state: {target}"
            )
        if parent_identity is None or link_identity is None or evidence != expected_evidence:
            raise SyncError(
                f"pending transaction {phase} claim binding is invalid: {target}"
            )
        evidence_snapshot = _read_symlink_snapshot_beneath(
            home,
            batch_root / Path(*evidence.parts),
        )
        if (
            evidence_snapshot.link_identity != link_identity
            or evidence_snapshot.link_target != link_target
        ):
            raise SyncError(
                f"pending transaction {phase} claim evidence changed: {target}"
            )
        claims.append(
            PendingLinkClaim(
                index=index,
                scope=scope,
                target=target,
                kind=kind,
                source=source,
                owner=owner,
                link_target=link_target,
                release_sha=release_sha,
                parent_identity=parent_identity,
                link_identity=link_identity,
                evidence=evidence,
            )
        )
    return tuple(claims)


def _read_pending_state_evidence(
    home: Path,
    batch_root: Path,
    raw: object,
    *,
    label: str,
    state_parent_identity: tuple[int, int],
    require_exists: bool,
    required_mode: int | None,
) -> tuple[ManagedStateFileSnapshot, PurePosixPath | None]:
    if not isinstance(raw, dict) or set(raw) != {
        "exists",
        "mode",
        "sha256",
        "identity",
        "evidence",
    }:
        raise SyncError(f"pending transaction {label} evidence is invalid")
    exists = raw.get("exists")
    if not isinstance(exists, bool) or (require_exists and not exists):
        raise SyncError(f"pending transaction {label} exists flag is invalid")
    mode = raw.get("mode")
    digest = raw.get("sha256")
    identity = _parse_pending_identity(
        raw.get("identity"),
        f"pending transaction {label} identity",
    )
    evidence = _parse_pending_relative_or_none(
        raw.get("evidence"),
        f"pending transaction {label} evidence path",
    )
    if not exists:
        if any(value is not None for value in (mode, digest, identity, evidence)):
            raise SyncError(f"missing pending transaction {label} has file metadata")
        return (
            ManagedStateFileSnapshot(
                exists=False,
                parent_identity=state_parent_identity,
            ),
            None,
        )
    if (
        not isinstance(mode, int)
        or isinstance(mode, bool)
        or mode < 0
        or mode > 0o7777
        or (required_mode is not None and mode != required_mode)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or identity is None
        or evidence is None
    ):
        raise SyncError(f"pending transaction {label} file metadata is invalid")
    evidence_path = batch_root / Path(*evidence.parts)
    parent_fd = _open_directory_beneath(home, evidence_path.parent)
    try:
        actual = _read_managed_state_file_snapshot(
            home,
            evidence_path,
            parent_fd,
            expected_identity=identity,
        )
    finally:
        _close_fd_quietly(parent_fd)
    if (
        not actual.exists
        or actual.mode != mode
        or actual.file_identity != identity
        or _snapshot_payload_digest(actual) != digest
    ):
        raise SyncError(f"pending transaction {label} evidence changed")
    return (
        ManagedStateFileSnapshot(
            exists=True,
            payload=actual.payload,
            mode=actual.mode,
            parent_identity=state_parent_identity,
            file_identity=actual.file_identity,
        ),
        evidence,
    )


def _parse_pending_release_expectations(
    raw: object,
    state: ManagedState,
    *,
    phase: str,
) -> tuple[PendingReleaseExpectation, ...]:
    expected_owners = sorted(state.owners.items())
    if (
        not isinstance(raw, list)
        or len(raw) > MAX_PENDING_RELEASES
        or len(raw) != len(expected_owners)
    ):
        raise SyncError(
            f"pending transaction {phase} release expectations do not match state"
        )
    expected_fields = {
        "owner",
        "sha",
        "directory_identity",
        "tree_sha256",
    }
    expectations: list[PendingReleaseExpectation] = []
    for index, (value, expected_owner) in enumerate(
        zip(raw, expected_owners)
    ):
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise SyncError(
                f"pending transaction {phase} release expectation "
                f"#{index + 1} is invalid"
            )
        owner = _validate_owner(
            value.get("owner"),
            f"pending {phase} release owner",
        )
        sha = _validate_release_sha(
            value.get("sha"),
            f"pending {phase} release SHA for owner {owner}",
        )
        directory_identity = _parse_pending_identity(
            value.get("directory_identity"),
            f"pending {phase} release directory identity",
        )
        tree_sha256 = value.get("tree_sha256")
        if (
            (owner, sha) != expected_owner
            or directory_identity is None
            or not isinstance(tree_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", tree_sha256) is None
        ):
            raise SyncError(
                f"pending transaction {phase} release expectation "
                f"does not match state: {owner}@{sha}"
            )
        expectations.append(
            PendingReleaseExpectation(
                owner=owner,
                sha=sha,
                directory_identity=directory_identity,
                tree_sha256=tree_sha256,
            )
        )
    return tuple(expectations)


def _read_pending_commit_evidence(
    home: Path,
    batch_root: Path,
    raw: object,
    *,
    state_parent_identity: tuple[int, int],
    state_after: ManagedStateFileSnapshot,
) -> tuple[ManagedStateFileSnapshot, PurePosixPath]:
    snapshot, evidence = _read_pending_state_evidence(
        home,
        batch_root,
        raw,
        label="commit",
        state_parent_identity=state_parent_identity,
        require_exists=True,
        required_mode=0o600,
    )
    if evidence != PENDING_STATE_COMMIT_EVIDENCE:
        raise SyncError("pending transaction commit evidence path is not canonical")
    if snapshot.payload is None:
        raise SyncError("pending transaction commit evidence payload is missing")
    data = _decode_managed_state_json(
        snapshot.payload,
        batch_root / Path(*evidence.parts),
    )
    if set(data) != {
        "version",
        "batch",
        "state_parent_identity",
        "state_after_identity",
        "state_after_sha256",
    }:
        raise SyncError("pending transaction commit evidence payload is invalid")
    version = data.get("version")
    if type(version) is not int or version != 1:
        raise SyncError("pending transaction commit evidence payload is invalid")
    if data.get("batch") != batch_root.name:
        raise SyncError("pending transaction commit evidence batch changed")
    if (
        _parse_pending_identity(
            data.get("state_parent_identity"),
            "pending commit state parent identity",
        )
        != state_parent_identity
        or _parse_pending_identity(
            data.get("state_after_identity"),
            "pending commit state-after identity",
        )
        != state_after.file_identity
        or data.get("state_after_sha256") != _snapshot_payload_digest(state_after)
    ):
        raise SyncError("pending transaction commit evidence state binding changed")
    return snapshot, evidence


def _parse_pending_link_batch(
    home: Path,
    payload: bytes,
    pointer_snapshot: ManagedStateFileSnapshot,
) -> PendingLinkBatch:
    data = _decode_managed_state_json(payload, _pending_link_pointer_path(home))
    if set(data) != {
        "version",
        "batch",
        "state_parent_identity",
        "state_before",
        "state_after",
        "releases_before",
        "releases_after",
        "commit_evidence",
        "commit_marker",
        "claims_before",
        "claims_after",
        "records",
    }:
        raise SyncError("pending transaction has unsupported fields or version")
    version = data.get("version")
    if type(version) is not int or version != 4:
        raise SyncError("pending transaction has unsupported fields or version")
    batch_name = data.get("batch")
    if (
        not isinstance(batch_name, str)
        or len(batch_name) > MAX_PENDING_LINK_BATCH_NAME_BYTES
        or PENDING_LINK_BATCH_RE.fullmatch(batch_name) is None
    ):
        raise SyncError("pending transaction has an invalid batch name")
    batch_root = _personal_sync_root(home) / QUARANTINE_RELATIVE_PATH / batch_name
    metadata_path = batch_root / PENDING_LINK_METADATA_NAME
    metadata_parent_fd = _open_directory_beneath(home, batch_root)
    try:
        batch_root_identity = _directory_identity(metadata_parent_fd)
        if not _bound_directory_matches(home, batch_root, metadata_parent_fd):
            raise SyncError("pending transaction batch root changed")
        batch_snapshot = _read_managed_state_file_snapshot(
            home,
            metadata_path,
            metadata_parent_fd,
            expected_identity=pointer_snapshot.file_identity,
        )
    finally:
        _close_fd_quietly(metadata_parent_fd)
    if (
        not batch_snapshot.exists
        or batch_snapshot.file_identity != pointer_snapshot.file_identity
        or batch_snapshot.payload != pointer_snapshot.payload
    ):
        raise SyncError("pending transaction pointer is not bound to its batch record")
    state_parent_identity = _parse_pending_identity(
        data.get("state_parent_identity"),
        "pending state parent identity",
    )
    if state_parent_identity is None:
        raise SyncError("pending state parent identity is missing")
    state_before, state_before_evidence = _read_pending_state_evidence(
        home,
        batch_root,
        data.get("state_before"),
        label="state-before",
        state_parent_identity=state_parent_identity,
        require_exists=False,
        required_mode=None,
    )
    state_after, state_after_evidence = _read_pending_state_evidence(
        home,
        batch_root,
        data.get("state_after"),
        label="state-after",
        state_parent_identity=state_parent_identity,
        require_exists=True,
        required_mode=0o600,
    )
    if state_before.exists:
        if state_before_evidence != PENDING_STATE_BEFORE_EVIDENCE:
            raise SyncError(
                "pending transaction state-before evidence path is not canonical"
            )
    elif state_before_evidence is not None:
        raise SyncError("missing pending state-before has an evidence path")
    if state_after_evidence != PENDING_STATE_AFTER_EVIDENCE:
        raise SyncError(
            "pending transaction state-after evidence path is not canonical"
        )
    if (
        state_before.exists
        and state_before.file_identity == state_after.file_identity
    ):
        raise SyncError(
            "pending transaction state-before and state-after evidence identities "
            "must differ"
        )
    assert state_after.payload is not None
    manifest_entry_indexes: dict[
        tuple[str, str],
        dict[tuple[PurePosixPath, PurePosixPath, str, str], LinkEntry],
    ] = {}
    state_before_value = _managed_state_value_from_snapshot(
        home,
        state_before,
        manifest_entry_indexes,
    )
    state_after_value = _managed_state_from_payload(
        home,
        _decode_managed_state_json(
            state_after.payload,
            batch_root / Path(*state_after_evidence.parts),
        ),
        manifest_entry_indexes,
    )
    releases_before = _parse_pending_release_expectations(
        data.get("releases_before"),
        state_before_value,
        phase="before",
    )
    releases_after = _parse_pending_release_expectations(
        data.get("releases_after"),
        state_after_value,
        phase="after",
    )
    releases_before_by_key = {
        (expectation.owner, expectation.sha): expectation
        for expectation in releases_before
    }
    for expectation in releases_after:
        matching_before = releases_before_by_key.get(
            (expectation.owner, expectation.sha)
        )
        if matching_before is not None and matching_before != expectation:
            raise SyncError(
                "pending transaction reused release expectation changed between "
                f"phases: {expectation.owner}@{expectation.sha}"
            )
    commit_evidence, commit_evidence_path = _read_pending_commit_evidence(
        home,
        batch_root,
        data.get("commit_evidence"),
        state_parent_identity=state_parent_identity,
        state_after=state_after,
    )
    if data.get("commit_marker") != PENDING_STATE_COMMIT_MARKER.as_posix():
        raise SyncError("pending transaction commit marker path is not canonical")
    raw_records = data.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > MAX_PENDING_LINK_RECORDS:
        raise SyncError("pending transaction records must be a bounded array")
    records: list[PendingLinkRecord] = []
    seen: set[tuple[str, PurePosixPath]] = set()
    expected_fields = {
        "index",
        "scope",
        "action",
        "target",
        "kind",
        "planned_before",
        "source",
        "owner",
        "link_target",
        "release_sha",
        "before_evidence",
        "before_evidence_identity",
        "backup",
        "stage",
        "stage_identity",
        "evidence",
        "evidence_identity",
    }
    for index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, dict) or set(raw_record) != expected_fields:
            raise SyncError(f"pending transaction record #{index + 1} is invalid")
        if raw_record.get("index") != index:
            raise SyncError("pending transaction record order changed")
        scope = raw_record.get("scope")
        action = raw_record.get("action")
        if scope not in {"current", "managed"} or action not in {
            "create",
            "replace",
            "quarantine-replace",
            "remove",
            "quarantine-remove",
            "retire-absent",
        }:
            raise SyncError(f"pending transaction record #{index + 1} has invalid role")
        target = _validate_relative_path(raw_record.get("target"), "pending target")
        kind = raw_record.get("kind")
        if kind not in {"file", "directory", "skill"}:
            raise SyncError(f"pending target {target} has unsupported kind")
        planned = _parse_pending_planned_snapshot(raw_record.get("planned_before"))
        producing = action in {"create", "replace", "quarantine-replace"}
        destructive = action in {
            "replace",
            "quarantine-replace",
            "remove",
            "quarantine-remove",
        }
        retiring_absence = action == "retire-absent"
        if retiring_absence:
            if (
                planned.link_identity is not None
                or planned.link_target is not None
            ):
                raise SyncError(
                    f"pending retired target {target} has invalid absence evidence"
                )
        elif destructive != (planned.link_identity is not None):
            raise SyncError(f"pending target {target} has inconsistent before evidence")
        source = _parse_pending_relative_or_none(raw_record.get("source"), "pending source")
        raw_owner = raw_record.get("owner")
        owner = None if raw_owner is None else _validate_owner(raw_owner, "pending owner")
        link_target = raw_record.get("link_target")
        if link_target is not None and (not isinstance(link_target, str) or not link_target):
            raise SyncError(f"pending target {target} has invalid link target")
        raw_sha = raw_record.get("release_sha")
        release_sha = None if raw_sha is None else _validate_release_sha(raw_sha)
        leaf = f"{index:08d}"
        before_evidence = _parse_pending_relative_or_none(
            raw_record.get("before_evidence"),
            "pending before evidence",
        )
        before_identity = _parse_pending_identity(
            raw_record.get("before_evidence_identity"),
            "pending before evidence identity",
        )
        backup = _parse_pending_relative_or_none(raw_record.get("backup"), "pending backup")
        stage = _parse_pending_relative_or_none(raw_record.get("stage"), "pending stage")
        stage_identity = _parse_pending_identity(
            raw_record.get("stage_identity"),
            "pending stage identity",
        )
        evidence = _parse_pending_relative_or_none(raw_record.get("evidence"), "pending evidence")
        evidence_identity = _parse_pending_identity(
            raw_record.get("evidence_identity"),
            "pending evidence identity",
        )
        if destructive:
            if (
                before_evidence != PurePosixPath("pending", "before", leaf)
                or before_identity != planned.link_identity
                or backup != PurePosixPath("links") / target
            ):
                raise SyncError(f"pending target {target} has invalid preimage paths")
            assert planned.link_target is not None
            before_snapshot = _read_symlink_snapshot_beneath(
                home,
                batch_root / Path(*before_evidence.parts),
            )
            if (
                before_snapshot.link_identity != before_identity
                or before_snapshot.link_target != planned.link_target
            ):
                raise SyncError(f"pending target {target} before evidence changed")
        elif any(value is not None for value in (before_evidence, before_identity, backup)):
            raise SyncError(f"pending create {target} has destructive evidence")
        if producing:
            if (
                stage != PurePosixPath("pending", "stage", leaf)
                or evidence != PurePosixPath("pending", "evidence", leaf)
                or stage_identity is None
                or evidence_identity != stage_identity
                or link_target is None
            ):
                raise SyncError(f"pending target {target} has invalid produced evidence")
            stage_snapshot = _read_symlink_snapshot_beneath(
                home,
                batch_root / Path(*stage.parts),
            )
            evidence_snapshot = _read_symlink_snapshot_beneath(
                home,
                batch_root / Path(*evidence.parts),
            )
            if (
                stage_snapshot.link_identity != stage_identity
                or evidence_snapshot.link_identity != evidence_identity
                or stage_snapshot.link_target != link_target
                or evidence_snapshot.link_target != link_target
            ):
                raise SyncError(f"pending target {target} produced evidence changed")
        elif any(value is not None for value in (stage, stage_identity, evidence, evidence_identity, link_target)):
            raise SyncError(f"pending removal {target} has produced evidence")

        if scope == "current":
            if kind != "directory" or owner is None or source is not None:
                raise SyncError(f"pending current record is invalid: {target}")
            expected_current = PurePosixPath(*_current_link(home, owner).relative_to(home).parts)
            if target != expected_current:
                raise SyncError(f"pending current path is invalid for owner {owner}")
            if retiring_absence and owner not in state_before_value.owners:
                raise SyncError(
                    f"pending retired current target is not an exact state transition: "
                    f"{target}"
                )
            if producing:
                if release_sha is None or link_target != f"releases/{release_sha}":
                    raise SyncError(f"pending current release is invalid for owner {owner}")
                if state_after_value.owners.get(owner) != release_sha:
                    raise SyncError(f"pending current state claim changed for owner {owner}")
            elif release_sha is not None or owner in state_after_value.owners:
                raise SyncError(f"pending current removal still has an owner claim: {owner}")
        elif retiring_absence:
            before_record = state_before_value.links.get(target)
            if (
                before_record is None
                or before_record.kind != kind
                or target in state_after_value.links
                or any(value is not None for value in (source, owner, release_sha))
            ):
                raise SyncError(
                    f"pending retired target is not an exact state transition: {target}"
                )
        elif producing:
            if source is None or owner is None or release_sha is None:
                raise SyncError(f"pending managed record is incomplete: {target}")
            cache_key = (owner, release_sha)
            entry_index = manifest_entry_indexes.get(cache_key)
            if entry_index is None:
                _ensure_safe_release_directory(home, owner, release_sha, allow_missing=False)
                manifest = _load_installed_manifest_data(home, owner, release_sha)
                entry_index = {
                    (entry.source, entry.target, entry.kind, entry.owner): entry
                    for entry in manifest.entries
                }
                manifest_entry_indexes[cache_key] = entry_index
            matching_entry = entry_index.get(
                (source, target, kind, owner),
            )
            if (
                matching_entry is None
                or _desired_link_target(home, matching_entry) != link_target
            ):
                raise SyncError(f"pending managed claim is not declared: {target}")
            expected_record = ManagedLinkRecord(
                source=source,
                target=target,
                kind=kind,
                owner=owner,
                link_target=link_target,
                release_sha=release_sha,
            )
            if state_after_value.links.get(target) != expected_record:
                raise SyncError(f"pending managed state claim changed: {target}")
        else:
            if any(value is not None for value in (source, owner, release_sha)):
                raise SyncError(f"pending managed removal has a state claim: {target}")
            if target in state_after_value.links:
                raise SyncError(f"pending managed removal remains claimed: {target}")

        key = (scope, target)
        if key in seen:
            raise SyncError(f"duplicate pending transaction target: {target}")
        seen.add(key)
        records.append(
            PendingLinkRecord(
                index=index,
                scope=scope,
                action=action,
                target=target,
                kind=kind,
                planned_snapshot=planned,
                source=source,
                owner=owner,
                link_target=link_target,
                release_sha=release_sha,
                before_evidence=before_evidence,
                before_evidence_identity=before_identity,
                backup=backup,
                stage=stage,
                stage_identity=stage_identity,
                evidence=evidence,
                evidence_identity=evidence_identity,
            )
        )
    for record in records:
        if record.action == "create":
            _require_pending_record_bound_create_absence(record)
        elif record.action == "retire-absent":
            _require_pending_record_bound_retired_absence(record)
    before_semantic_keys = {
        (semantic[0], semantic[1])
        for semantic in _pending_state_claim_semantics(home, state_before_value)
    }
    state_claimed_absences = {
        (record.scope, record.target)
        for record in records
        if record.action in {"create", "retire-absent"}
        and (record.scope, record.target) in before_semantic_keys
    }
    claims_before = _parse_pending_link_claims(
        home,
        batch_root,
        "before",
        data.get("claims_before"),
        state_before_value,
        omitted_keys=state_claimed_absences,
    )
    claims_after = _parse_pending_link_claims(
        home,
        batch_root,
        "after",
        data.get("claims_after"),
        state_after_value,
    )
    before_claims_by_target = {
        (claim.scope, claim.target): claim for claim in claims_before
    }
    after_claims_by_target = {
        (claim.scope, claim.target): claim for claim in claims_after
    }
    for record in records:
        key = (record.scope, record.target)
        before_claim = before_claims_by_target.get(key)
        after_claim = after_claims_by_target.get(key)
        destructive = record.action in {
            "replace",
            "quarantine-replace",
            "remove",
            "quarantine-remove",
        }
        producing = record.action in {
            "create",
            "replace",
            "quarantine-replace",
        }
        if (
            record.action in {"create", "retire-absent"}
            and key in before_semantic_keys
        ):
            if before_claim is not None:
                raise SyncError(
                    "pending before-state absence overlaps a symlink claim: "
                    f"{record.target}"
                )
        elif before_claim is not None and (
            not destructive
            or before_claim.parent_identity != record.planned_snapshot.parent_identity
            or before_claim.link_identity != record.planned_snapshot.link_identity
            or before_claim.link_target != record.planned_snapshot.link_target
        ):
            raise SyncError(
                f"pending before-state action claim is inconsistent: {record.target}"
            )
        if producing:
            if after_claim is None or (
                after_claim.parent_identity != record.planned_snapshot.parent_identity
                or after_claim.link_identity != record.evidence_identity
                or after_claim.link_target != record.link_target
            ):
                raise SyncError(
                    f"pending after-state action claim is inconsistent: {record.target}"
                )
        elif after_claim is not None:
            raise SyncError(
                f"pending removed target still has an after-state claim: {record.target}"
            )
    assert state_after_evidence is not None
    return PendingLinkBatch(
        batch_root=batch_root,
        batch_root_identity=batch_root_identity,
        records=tuple(records),
        claims_before=claims_before,
        claims_after=claims_after,
        state_before=state_before,
        state_after=state_after,
        state_before_evidence=state_before_evidence,
        state_after_evidence=state_after_evidence,
        state_before_value=state_before_value,
        state_after_value=state_after_value,
        releases_before=releases_before,
        releases_after=releases_after,
        commit_evidence=commit_evidence,
        commit_evidence_path=commit_evidence_path,
        commit_marker_path=PENDING_STATE_COMMIT_MARKER,
        pointer_snapshot=pointer_snapshot,
    )


def _load_pending_link_batch(home: Path) -> PendingLinkBatch | None:
    pointer_path = _pending_link_pointer_path(home)
    try:
        parent_fd = _open_directory_beneath(home, home)
    except FileNotFoundError:
        return None
    try:
        snapshot = _read_managed_state_file_snapshot(home, pointer_path, parent_fd)
    finally:
        _close_fd_quietly(parent_fd)
    if not snapshot.exists:
        return None
    assert snapshot.payload is not None
    return _parse_pending_link_batch(home, snapshot.payload, snapshot)


def _publish_pending_link_pointer(home: Path, batch: PendingLinkBatch) -> None:
    pointer_path = _pending_link_pointer_path(home)
    metadata_path = batch.batch_root / PENDING_LINK_METADATA_NAME
    home_fd = _open_or_create_sync_home(home)
    source_parent_fd = -1
    target_parent_fd = -1
    published = False
    try:
        source_parent_fd = _open_directory_beneath(
            home,
            metadata_path.parent,
            home_fd=home_fd,
        )
        target_parent_fd = _open_directory_beneath(home, home, home_fd=home_fd)
        source_snapshot = _read_managed_state_file_snapshot(
            home,
            metadata_path,
            source_parent_fd,
        )
        if not source_snapshot.exists:
            raise SyncError("pending link batch record disappeared before publication")
        if _pending_commit_decision(home, batch):
            raise SyncError(
                "pending transaction was marked committed before publication"
            )
        _verify_pending_link_phase(home, batch, "before")
        try:
            os.link(
                metadata_path.name,
                pointer_path.name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise SyncError("another pending link transaction already exists") from error
        published = True
        target_snapshot = _read_managed_state_file_snapshot(
            home,
            pointer_path,
            target_parent_fd,
            expected_identity=source_snapshot.file_identity,
        )
        if (
            target_snapshot.file_identity != source_snapshot.file_identity
            or target_snapshot.payload != source_snapshot.payload
        ):
            raise SyncError("pending link pointer changed during publication")
        os.fsync(target_parent_fd)
        if (
            not _bound_directory_matches(home, metadata_path.parent, source_parent_fd)
            or not _bound_directory_matches(home, pointer_path.parent, target_parent_fd)
        ):
            raise SyncError("pending link pointer parent changed during publication")
        batch.pointer_snapshot = target_snapshot
    except BaseException as error:
        if published:
            try:
                _quarantine_pending_link_pointer(
                    home,
                    batch,
                    source_snapshot,
                    target_parent_fd,
                    label="publish-error",
                )
            except BaseException as cleanup_error:
                raise SyncError(
                    "pending link pointer publication failed and exact cleanup was "
                    f"incomplete: {cleanup_error}"
                ) from error
        raise
    finally:
        if target_parent_fd >= 0:
            _close_fd_quietly(target_parent_fd)
        if source_parent_fd >= 0:
            _close_fd_quietly(source_parent_fd)
        _close_fd_quietly(home_fd)


def _quarantine_pending_link_pointer(
    home: Path,
    batch: PendingLinkBatch,
    expected: ManagedStateFileSnapshot,
    parent_fd: int,
    *,
    label: str,
) -> Path:
    pointer_path = _pending_link_pointer_path(home)
    if expected.file_identity is None:
        raise SyncError("pending link pointer has no bound identity")
    transaction = ManagedStateFileTransaction(
        before=expected,
        after=ManagedStateFileSnapshot(exists=False),
        batch_root=batch.batch_root,
        state_parent_identity=_directory_identity(parent_fd),
    )
    moved, matches = _move_managed_state_entry_to_quarantine(
        home,
        transaction,
        parent_fd,
        pointer_path.name,
        f"pending-{label}",
        expected_identity=expected.file_identity,
        expected_snapshot=expected,
    )
    if not matches:
        raise SyncError(
            f"pending link pointer changed and was retained in quarantine: {moved}"
        )
    if _managed_state_name_exists(parent_fd, pointer_path.name):
        raise SyncError(
            f"pending link pointer reappeared and was left in place: {pointer_path}"
        )
    return moved


def _pending_commit_snapshot_matches(
    actual: ManagedStateFileSnapshot,
    expected: ManagedStateFileSnapshot,
) -> bool:
    return (
        actual.exists
        and expected.exists
        and actual.file_identity == expected.file_identity
        and actual.mode == expected.mode
        and actual.payload == expected.payload
    )


def _pending_commit_marker_snapshot(
    home: Path,
    batch: PendingLinkBatch,
) -> ManagedStateFileSnapshot | None:
    evidence_path = batch.batch_root / Path(*batch.commit_evidence_path.parts)
    marker_path = batch.batch_root / Path(*batch.commit_marker_path.parts)
    if evidence_path.parent != marker_path.parent:
        raise SyncError("pending transaction commit paths do not share a parent")
    parent_fd = _open_directory_beneath(home, evidence_path.parent)
    try:
        evidence = _read_managed_state_file_snapshot(
            home,
            evidence_path,
            parent_fd,
            expected_identity=batch.commit_evidence.file_identity,
        )
        if not _pending_commit_snapshot_matches(evidence, batch.commit_evidence):
            raise SyncError("pending transaction commit evidence changed")
        marker = _read_managed_state_file_snapshot(
            home,
            marker_path,
            parent_fd,
        )
        if not marker.exists:
            return None
        if not _pending_commit_snapshot_matches(marker, batch.commit_evidence):
            raise SyncError(
                "pending transaction commit marker is not the exact evidence inode"
            )
        return marker
    finally:
        _close_fd_quietly(parent_fd)


def _pending_commit_decision(home: Path, batch: PendingLinkBatch) -> bool:
    return _pending_commit_marker_snapshot(home, batch) is not None


def _publish_pending_commit_marker(home: Path, batch: PendingLinkBatch) -> None:
    if _pending_commit_decision(home, batch):
        raise SyncError("pending transaction commit marker already exists")
    evidence_path = batch.batch_root / Path(*batch.commit_evidence_path.parts)
    marker_path = batch.batch_root / Path(*batch.commit_marker_path.parts)
    if evidence_path.parent != marker_path.parent:
        raise SyncError("pending transaction commit paths do not share a parent")
    parent_fd = _open_directory_beneath(home, evidence_path.parent)
    try:
        evidence = _read_managed_state_file_snapshot(
            home,
            evidence_path,
            parent_fd,
            expected_identity=batch.commit_evidence.file_identity,
        )
        if not _pending_commit_snapshot_matches(evidence, batch.commit_evidence):
            raise SyncError("pending transaction commit evidence changed")
        try:
            os.link(
                evidence_path.name,
                marker_path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise SyncError(
                "pending transaction commit marker appeared during publication"
            ) from error
        os.fsync(parent_fd)
        marker = _read_managed_state_file_snapshot(
            home,
            marker_path,
            parent_fd,
            expected_identity=batch.commit_evidence.file_identity,
        )
        if not _pending_commit_snapshot_matches(marker, batch.commit_evidence):
            raise SyncError("pending transaction commit marker changed")
        if not _bound_directory_matches(home, marker_path.parent, parent_fd):
            raise SyncError("pending transaction commit marker parent changed")
    finally:
        _close_fd_quietly(parent_fd)


def _pending_cleanup_index_path(home: Path) -> Path:
    return _personal_sync_root(home) / PENDING_CLEANUP_INDEX_RELATIVE_PATH


def _pending_cleanup_ticket_path(home: Path, batch_name: str) -> Path:
    if (
        len(batch_name) > MAX_PENDING_LINK_BATCH_NAME_BYTES
        or PENDING_LINK_BATCH_RE.fullmatch(batch_name) is None
    ):
        raise SyncError("pending cleanup ticket has an invalid batch name")
    return _pending_cleanup_index_path(home) / (
        batch_name + PENDING_CLEANUP_TICKET_SUFFIX
    )


def _pending_cleanup_isolated_batch_name(batch_name: str) -> str:
    return PENDING_CLEANUP_ISOLATED_BATCH_PREFIX + batch_name


def _pending_cleanup_batch_name_from_quarantine_entry(
    entry_name: str,
) -> str | None:
    batch_name = entry_name
    if entry_name.startswith(PENDING_CLEANUP_ISOLATED_BATCH_PREFIX):
        batch_name = entry_name[len(PENDING_CLEANUP_ISOLATED_BATCH_PREFIX) :]
    if (
        len(batch_name) > MAX_PENDING_LINK_BATCH_NAME_BYTES
        or PENDING_LINK_BATCH_RE.fullmatch(batch_name) is None
    ):
        return None
    return batch_name


def _retained_pending_cleanup_names(path: Path):
    for _attempt in range(128):
        yield (
            f"{PENDING_CLEANUP_RETAINED_PREFIX}{path.name}-"
            f"{os.getpid()}-{os.urandom(8).hex()}"
        )
    raise SyncError("failed to allocate a retained pending cleanup name")


def _isolate_and_delete_pending_cleanup_file(
    home: Path,
    path: Path,
    parent_fd: int,
    expected: ManagedStateFileSnapshot,
    *,
    label: str,
) -> None:
    if (
        not expected.exists
        or expected.payload is None
        or expected.mode is None
        or expected.parent_identity != _directory_identity(parent_fd)
        or expected.file_identity is None
    ):
        raise SyncError(f"{label} has no deletion identity")
    if not _bound_directory_matches(home, path.parent, parent_fd):
        raise SyncError(f"{label} parent changed before isolation")

    retained_name: str | None = None
    for candidate in _retained_pending_cleanup_names(path):
        try:
            _rename_noreplace_at(
                parent_fd,
                path.name,
                parent_fd,
                candidate,
            )
        except FileExistsError:
            continue
        except FileNotFoundError as error:
            raise SyncError(f"{label} disappeared before isolation") from error
        retained_name = candidate
        break
    assert retained_name is not None
    os.fsync(parent_fd)

    retained_path = path.with_name(retained_name)
    file_fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(retained_name, flags, dir_fd=parent_fd)
            before = os.fstat(file_fd)
            payload = _read_managed_state_bytes(file_fd, retained_path)
            after = os.fstat(file_fd)
            named = os.stat(
                retained_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise SyncError(
                f"{label} changed before deletion; preserved as {retained_name}"
            ) from error
        if (
            not stat.S_ISREG(before.st_mode)
            or _managed_state_metadata_snapshot(before)
            != _managed_state_metadata_snapshot(after)
            or _managed_state_metadata_snapshot(named)
            != _managed_state_metadata_snapshot(after)
            or (after.st_dev, after.st_ino) != expected.file_identity
            or stat.S_IMODE(after.st_mode) != expected.mode
            or payload != expected.payload
            or not _bound_directory_matches(home, path.parent, parent_fd)
        ):
            raise SyncError(
                f"{label} changed before deletion; preserved as {retained_name}"
            )
        os.unlink(retained_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        if _named_entry_identity(parent_fd, retained_name) is not None:
            raise SyncError(
                f"{label} reappeared after deletion; preserved as {retained_name}"
            )
    finally:
        if file_fd >= 0:
            _close_fd_quietly(file_fd)


def _pending_cleanup_ticket_payload(
    batch_root: Path,
    batch_root_identity: tuple[int, int],
    marker_parent_identity: tuple[int, int],
    marker_file_identity: tuple[int, int],
    marker_mode: int,
    marker_sha256: str,
) -> bytes:
    return _bounded_json_document(
        {
            "version": 1,
            "batch": batch_root.name,
            "batch_root_identity": _identity_payload(batch_root_identity),
            "commit_marker": {
                "path": PENDING_STATE_COMMIT_MARKER.as_posix(),
                "parent_identity": _identity_payload(marker_parent_identity),
                "file_identity": _identity_payload(marker_file_identity),
                "mode": marker_mode,
                "sha256": marker_sha256,
            },
        },
        max_bytes=MAX_PENDING_CLEANUP_TICKET_BYTES,
        overflow_error="pending cleanup ticket exceeds the size limit",
    )


def _read_pending_cleanup_ticket(
    home: Path,
    ticket_path: Path,
    *,
    expected_ticket_identity: tuple[int, int] | None = None,
) -> PendingBatchCleanupTicket | None:
    suffix = PENDING_CLEANUP_TICKET_SUFFIX
    if not ticket_path.name.endswith(suffix):
        raise SyncError("pending cleanup ticket has an invalid file name")
    batch_name = ticket_path.name[: -len(suffix)]
    if (
        len(batch_name) > MAX_PENDING_LINK_BATCH_NAME_BYTES
        or PENDING_LINK_BATCH_RE.fullmatch(batch_name) is None
    ):
        raise SyncError("pending cleanup ticket has an invalid batch name")
    index_fd = _open_directory_beneath(home, ticket_path.parent)
    try:
        snapshot = _read_managed_state_file_snapshot(
            home,
            ticket_path,
            index_fd,
            expected_identity=expected_ticket_identity,
        )
        if not snapshot.exists:
            return None
        if snapshot.payload is None or len(snapshot.payload) > MAX_PENDING_CLEANUP_TICKET_BYTES:
            raise SyncError(f"pending cleanup ticket exceeds its limit: {batch_name}")
        if snapshot.mode != 0o600:
            raise SyncError(f"pending cleanup ticket mode changed: {batch_name}")
        data = _decode_managed_state_json(snapshot.payload, ticket_path)
        if set(data) != {
            "version",
            "batch",
            "batch_root_identity",
            "commit_marker",
        }:
            raise SyncError(
                f"pending cleanup ticket has unsupported fields: {batch_name}"
            )
        version = data.get("version")
        if type(version) is not int or version != 1:
            raise SyncError(f"pending cleanup ticket has unsupported fields: {batch_name}")
        if data.get("batch") != batch_name:
            raise SyncError(f"pending cleanup ticket batch changed: {batch_name}")
        batch_identity = _parse_pending_identity(
            data.get("batch_root_identity"),
            "pending cleanup batch root identity",
        )
        if batch_identity is None:
            raise SyncError(
                f"pending cleanup batch identity is missing: {batch_name}"
            )
        marker = data.get("commit_marker")
        if (
            not isinstance(marker, dict)
            or set(marker)
            != {
                "path",
                "parent_identity",
                "file_identity",
                "mode",
                "sha256",
            }
            or marker.get("path") != PENDING_STATE_COMMIT_MARKER.as_posix()
        ):
            raise SyncError(f"pending cleanup commit marker changed: {batch_name}")
        marker_parent_identity = _parse_pending_identity(
            marker.get("parent_identity"),
            "pending cleanup commit marker parent identity",
        )
        marker_file_identity = _parse_pending_identity(
            marker.get("file_identity"),
            "pending cleanup commit marker file identity",
        )
        if marker_parent_identity is None or marker_file_identity is None:
            raise SyncError(
                f"pending cleanup commit marker identity is missing: {batch_name}"
            )
        marker_mode = marker.get("mode")
        marker_sha256 = marker.get("sha256")
        if marker_mode != 0o600:
            raise SyncError(f"pending cleanup commit marker mode changed: {batch_name}")
        if (
            not isinstance(marker_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", marker_sha256) is None
        ):
            raise SyncError(f"pending cleanup commit marker digest changed: {batch_name}")
        batch_root = (
            _personal_sync_root(home)
            / QUARANTINE_RELATIVE_PATH
            / batch_name
        )
        expected_payload = _pending_cleanup_ticket_payload(
            batch_root,
            batch_identity,
            marker_parent_identity,
            marker_file_identity,
            marker_mode,
            marker_sha256,
        )
        if snapshot.payload != expected_payload:
            raise SyncError(f"pending cleanup ticket changed: {batch_name}")
        return PendingBatchCleanupTicket(
            path=ticket_path,
            snapshot=snapshot,
            batch_root=batch_root,
            batch_root_identity=batch_identity,
            marker_parent_identity=marker_parent_identity,
            marker_file_identity=marker_file_identity,
            marker_mode=marker_mode,
            marker_sha256=marker_sha256,
        )
    finally:
        _close_fd_quietly(index_fd)


def _discard_incomplete_pending_cleanup_ticket(
    home: Path,
    temp_path: Path,
) -> None:
    index_fd = _open_directory_beneath(home, temp_path.parent)
    try:
        snapshot = _read_managed_state_file_snapshot(
            home,
            temp_path,
            index_fd,
        )
        if not snapshot.exists:
            return
        if snapshot.mode != 0o600:
            raise SyncError(
                f"incomplete pending cleanup ticket mode changed: {temp_path.name}"
            )
        _isolate_and_delete_pending_cleanup_file(
            home,
            temp_path,
            index_fd,
            snapshot,
            label=f"incomplete pending cleanup ticket {temp_path.name}",
        )
    finally:
        _close_fd_quietly(index_fd)


def _publish_pending_cleanup_ticket(
    home: Path,
    ticket_path: Path,
    payload: bytes,
) -> ManagedStateFileSnapshot:
    # The final ticket name is published only after a fully written temporary
    # file has been fsynced. A crash-truncated temp is not deletion authority
    # and can be discarded by the next lock holder before retrying publication.
    temp_path = ticket_path.with_name(ticket_path.name + ".tmp")
    _discard_incomplete_pending_cleanup_ticket(home, temp_path)
    staged = _write_exclusive_internal_file(home, temp_path, payload)
    index_fd = _open_directory_beneath(home, ticket_path.parent)
    try:
        current_temp = _read_managed_state_file_snapshot(
            home,
            temp_path,
            index_fd,
            expected_identity=staged.file_identity,
        )
        if current_temp != staged or current_temp.payload != payload:
            raise SyncError("pending cleanup ticket changed before publication")
        try:
            _rename_noreplace_at(
                index_fd,
                temp_path.name,
                index_fd,
                ticket_path.name,
            )
        except FileExistsError:
            existing = _read_managed_state_file_snapshot(
                home,
                ticket_path,
                index_fd,
            )
            if not existing.exists or existing.payload != payload:
                raise SyncError(
                    "pending cleanup ticket appeared with changed content"
                )
            current_temp = _read_managed_state_file_snapshot(
                home,
                temp_path,
                index_fd,
                expected_identity=staged.file_identity,
            )
            if current_temp != staged:
                raise SyncError(
                    "pending cleanup ticket temp changed after publication race"
                )
            _isolate_and_delete_pending_cleanup_file(
                home,
                temp_path,
                index_fd,
                staged,
                label="pending cleanup ticket temp",
            )
            return existing
        os.fsync(index_fd)
        published = _read_managed_state_file_snapshot(
            home,
            ticket_path,
            index_fd,
            expected_identity=staged.file_identity,
        )
        if published.payload != payload or published.mode != 0o600:
            raise SyncError("pending cleanup ticket changed during publication")
        return published
    finally:
        _close_fd_quietly(index_fd)


def _verify_pending_cleanup_ticket_durable(
    home: Path,
    ticket: PendingBatchCleanupTicket,
) -> None:
    index_fd = _open_directory_beneath(home, ticket.path.parent)
    index_parent_fd = -1
    try:
        index_parent_fd = _open_directory_beneath(
            home,
            ticket.path.parent.parent,
        )
        if not _bound_directory_matches(home, ticket.path.parent, index_fd):
            raise SyncError("pending cleanup index changed before fsync")
        if not _bound_directory_matches(
            home,
            ticket.path.parent.parent,
            index_parent_fd,
        ):
            raise SyncError("pending cleanup index parent changed before fsync")
        os.fsync(index_fd)
        os.fsync(index_parent_fd)
        durable = _read_pending_cleanup_ticket(
            home,
            ticket.path,
            expected_ticket_identity=ticket.snapshot.file_identity,
        )
        if durable != ticket:
            raise SyncError("pending cleanup ticket changed after fsync")
    finally:
        if index_parent_fd >= 0:
            _close_fd_quietly(index_parent_fd)
        _close_fd_quietly(index_fd)


def _mark_pending_batch_cleanup_ready(
    home: Path,
    batch: PendingLinkBatch,
) -> None:
    marker = _pending_commit_marker_snapshot(home, batch)
    if marker is None:
        raise SyncError("refusing to mark an uncommitted pending batch for cleanup")
    if (
        not marker.exists
        or marker.parent_identity is None
        or marker.file_identity is None
        or marker.payload is None
        or marker.mode != 0o600
    ):
        raise SyncError("pending cleanup commit marker is not fully bound")
    index_fd = _open_or_create_directory_beneath(
        home,
        _pending_cleanup_index_path(home),
        mode=0o700,
    )
    _close_fd_quietly(index_fd)
    ticket_path = _pending_cleanup_ticket_path(home, batch.batch_root.name)
    existing = _read_pending_cleanup_ticket(
        home,
        ticket_path,
    )
    expected_payload = _pending_cleanup_ticket_payload(
        batch.batch_root,
        batch.batch_root_identity,
        marker.parent_identity,
        marker.file_identity,
        marker.mode,
        hashlib.sha256(marker.payload).hexdigest(),
    )
    if existing is not None:
        if existing.snapshot.payload != expected_payload:
            raise SyncError("pending cleanup ticket payload changed")
        _verify_pending_cleanup_ticket_durable(home, existing)
        return
    published = _publish_pending_cleanup_ticket(
        home,
        ticket_path,
        expected_payload,
    )
    if published.payload != expected_payload:
        raise SyncError("pending cleanup ticket changed during publication")
    verified = _read_pending_cleanup_ticket(
        home,
        ticket_path,
        expected_ticket_identity=published.file_identity,
    )
    if verified is None or verified.snapshot != published:
        raise SyncError("pending cleanup ticket changed after publication")
    _verify_pending_cleanup_ticket_durable(home, verified)


def _pending_link_pointer_is_absent(home: Path) -> bool:
    try:
        home_fd = _open_directory_beneath(home, home)
    except FileNotFoundError:
        return True
    try:
        snapshot = _read_managed_state_file_snapshot(
            home,
            _pending_link_pointer_path(home),
            home_fd,
        )
        return not snapshot.exists
    finally:
        _close_fd_quietly(home_fd)


def _directory_mount_identity(directory_fd: int) -> tuple[int, int | None]:
    device = os.fstat(directory_fd).st_dev
    if sys.platform == "darwin":
        # macOS has no Linux-style bind-mount primitive in the supported
        # installer environment. Pair st_dev with the filesystem id so other
        # mounted filesystems are still rejected before recursion.
        filesystem_id = getattr(os.fstatvfs(directory_fd), "f_fsid", None)
        if not isinstance(filesystem_id, int):
            raise SyncError("pending cleanup macOS filesystem id is missing")
        return device, filesystem_id
    if not sys.platform.startswith("linux"):
        raise SyncError(
            "pending cleanup mount identity is unsupported on this platform"
        )
    fdinfo_path = Path("/proc/self/fdinfo") / str(directory_fd)
    fdinfo_fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fdinfo_fd = os.open(fdinfo_path, flags)
        raw_payload = os.read(fdinfo_fd, 8193)
        if len(raw_payload) > 8192:
            raise SyncError("pending cleanup Linux fdinfo exceeds its limit")
        payload = raw_payload.decode("ascii")
    except (OSError, UnicodeError) as error:
        raise SyncError("pending cleanup cannot verify the Linux mount id") from error
    finally:
        if fdinfo_fd >= 0:
            _close_fd_quietly(fdinfo_fd)
    mount_id: int | None = None
    for line in payload.splitlines():
        if line.startswith("mnt_id:"):
            raw_mount_id = line.partition(":")[2].strip()
            if raw_mount_id.isdecimal():
                mount_id = int(raw_mount_id)
            break
    if mount_id is None:
        raise SyncError("pending cleanup Linux mount id is missing")
    return device, mount_id


def _pending_cleanup_entry_plan(
    metadata: os.stat_result,
) -> tuple[int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def _pending_cleanup_entry_name(
    prefix: str,
    parent_identity: tuple[int, int],
    planned: tuple[int, int, int],
) -> str:
    return (
        f"{prefix}{parent_identity[0]:x}-{parent_identity[1]:x}-"
        f"{planned[0]:x}-{planned[1]:x}-{planned[2]:x}-"
        f"{os.urandom(8).hex()}"
    )


def _pending_cleanup_internal_entry_plan(
    name: str,
    prefix: str,
    parent_identity: tuple[int, int],
) -> tuple[int, int, int] | None:
    if not name.startswith(prefix):
        return None
    match = PENDING_CLEANUP_ENTRY_TOKEN_RE.fullmatch(name[len(prefix) :])
    if match is None:
        return None
    parsed_parent = (int(match.group(1), 16), int(match.group(2), 16))
    if parsed_parent != parent_identity:
        return None
    return (
        int(match.group(3), 16),
        int(match.group(4), 16),
        int(match.group(5), 16),
    )


def _retain_pending_cleanup_entry(
    directory_fd: int,
    name: str,
    parent_identity: tuple[int, int],
    planned: tuple[int, int, int],
    *,
    label: str,
) -> NoReturn:
    for _attempt in range(128):
        retained_name = _pending_cleanup_entry_name(
            PENDING_CLEANUP_RETAINED_ENTRY_PREFIX,
            parent_identity,
            planned,
        )
        try:
            _rename_noreplace_at(
                directory_fd,
                name,
                directory_fd,
                retained_name,
            )
        except FileExistsError:
            continue
        except FileNotFoundError as error:
            raise SyncError(f"{label}; isolated entry disappeared") from error
        os.fsync(directory_fd)
        raise SyncError(f"{label}; preserved as {retained_name}")
    raise SyncError("failed to allocate a retained pending cleanup entry name")


def _isolate_pending_cleanup_entry(
    directory_fd: int,
    name: str,
    parent_identity: tuple[int, int],
    planned: tuple[int, int, int],
) -> tuple[str, os.stat_result]:
    for _attempt in range(128):
        active_name = _pending_cleanup_entry_name(
            PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
            parent_identity,
            planned,
        )
        if active_name == name:
            continue
        try:
            _rename_noreplace_at(
                directory_fd,
                name,
                directory_fd,
                active_name,
            )
        except FileExistsError:
            continue
        except FileNotFoundError as error:
            raise SyncError(
                f"pending cleanup entry disappeared before isolation: {name}"
            ) from error
        os.fsync(directory_fd)
        break
    else:
        raise SyncError("failed to allocate an active pending cleanup entry name")
    try:
        current = os.stat(
            active_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise SyncError(
            f"pending cleanup isolated entry changed: {active_name}"
        ) from error
    if _pending_cleanup_entry_plan(current) != planned:
        _retain_pending_cleanup_entry(
            directory_fd,
            active_name,
            parent_identity,
            planned,
            label=f"pending cleanup entry changed before deletion: {name}",
        )
    # The verified active name is high entropy and exists only inside the
    # locked, mode-0700 sync quarantine. Portable Unix APIs do not offer an
    # inode-conditional unlink; a same-uid process deliberately observing and
    # replacing this internal name is outside the installer threat model
    # because it already has equivalent authority over sync state and releases.
    return active_name, current


def _remove_pending_batch_directory_contents(
    directory_fd: int,
    directory_identity: tuple[int, int],
    root_mount_identity: tuple[int, int | None],
    budget: list[int],
    *,
    depth: int,
    skipped_names: frozenset[str] = frozenset(),
) -> None:
    if depth > MAX_PENDING_CLEANUP_DEPTH:
        raise SyncError("pending cleanup directory depth exceeds the limit")
    if _directory_identity(directory_fd) != directory_identity:
        raise SyncError("pending cleanup directory identity changed")
    if _directory_mount_identity(directory_fd) != root_mount_identity:
        raise SyncError("pending cleanup directory crosses a mount boundary")
    entries: list[tuple[str, tuple[int, int, int], bool]] = []
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            if entry.name in skipped_names:
                continue
            if budget[0] <= 0:
                raise SyncError("pending cleanup entry count exceeds the limit")
            budget[0] -= 1
            retained_plan = _pending_cleanup_internal_entry_plan(
                entry.name,
                PENDING_CLEANUP_RETAINED_ENTRY_PREFIX,
                directory_identity,
            )
            if retained_plan is not None:
                raise SyncError(
                    "pending cleanup retained entry requires manual cleanup: "
                    f"{entry.name}"
                )
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SyncError(
                    f"pending cleanup entry changed: {entry.name}"
                ) from error
            if metadata.st_dev != root_mount_identity[0]:
                raise SyncError(
                    f"pending cleanup entry crosses a device boundary: {entry.name}"
                )
            active_plan = _pending_cleanup_internal_entry_plan(
                entry.name,
                PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                directory_identity,
            )
            entries.append(
                (
                    entry.name,
                    active_plan or _pending_cleanup_entry_plan(metadata),
                    active_plan is not None,
                )
            )

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    mutated = False
    for name, planned, already_active in entries:
        if planned[2] not in {
            stat.S_IFDIR,
            stat.S_IFREG,
            stat.S_IFLNK,
        }:
            raise SyncError(f"pending cleanup found an unsupported entry: {name}")
        if planned[2] == stat.S_IFDIR and not already_active:
            preflight_fd = -1
            try:
                preflight_fd = os.open(
                    name,
                    directory_flags,
                    dir_fd=directory_fd,
                )
            except OSError:
                pass
            else:
                try:
                    if (
                        _directory_identity(preflight_fd) == planned[:2]
                        and _directory_mount_identity(preflight_fd)
                        != root_mount_identity
                    ):
                        raise SyncError(
                            "pending cleanup child crosses a mount boundary: "
                            f"{name}"
                        )
                finally:
                    _close_fd_quietly(preflight_fd)
        active_name, current = _isolate_pending_cleanup_entry(
            directory_fd,
            name,
            directory_identity,
            planned,
        )
        mutated = True
        if stat.S_ISDIR(current.st_mode):
            try:
                child_fd = os.open(
                    active_name,
                    directory_flags,
                    dir_fd=directory_fd,
                )
            except OSError:
                _retain_pending_cleanup_entry(
                    directory_fd,
                    active_name,
                    directory_identity,
                    planned,
                    label=f"pending cleanup child directory changed: {name}",
                )
            try:
                child_identity = _directory_identity(child_fd)
                if child_identity != planned[:2]:
                    _retain_pending_cleanup_entry(
                        directory_fd,
                        active_name,
                        directory_identity,
                        planned,
                        label=f"pending cleanup child directory changed: {name}",
                    )
                if _directory_mount_identity(child_fd) != root_mount_identity:
                    _retain_pending_cleanup_entry(
                        directory_fd,
                        active_name,
                        directory_identity,
                        planned,
                        label=(
                            "pending cleanup child crosses a mount boundary: "
                            f"{name}"
                        ),
                    )
                _remove_pending_batch_directory_contents(
                    child_fd,
                    child_identity,
                    root_mount_identity,
                    budget,
                    depth=depth + 1,
                )
                try:
                    current = os.stat(
                        active_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    _retain_pending_cleanup_entry(
                        directory_fd,
                        active_name,
                        directory_identity,
                        planned,
                        label=f"pending cleanup child directory changed: {name}",
                    )
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or _pending_cleanup_entry_plan(current) != planned
                    or _directory_identity(child_fd) != child_identity
                ):
                    _retain_pending_cleanup_entry(
                        directory_fd,
                        active_name,
                        directory_identity,
                        planned,
                        label=f"pending cleanup child directory changed: {name}",
                    )
                try:
                    os.rmdir(active_name, dir_fd=directory_fd)
                except OSError:
                    _retain_pending_cleanup_entry(
                        directory_fd,
                        active_name,
                        directory_identity,
                        planned,
                        label=f"pending cleanup child deletion changed: {name}",
                    )
            finally:
                _close_fd_quietly(child_fd)
        elif stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
            try:
                os.unlink(active_name, dir_fd=directory_fd)
            except OSError:
                _retain_pending_cleanup_entry(
                    directory_fd,
                    active_name,
                    directory_identity,
                    planned,
                    label=f"pending cleanup entry deletion changed: {name}",
                )
        try:
            os.stat(
                active_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            _retain_pending_cleanup_entry(
                directory_fd,
                active_name,
                directory_identity,
                planned,
                label=f"pending cleanup entry reappeared: {name}",
            )

    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            if entry.name in skipped_names:
                continue
            retained_plan = _pending_cleanup_internal_entry_plan(
                entry.name,
                PENDING_CLEANUP_RETAINED_ENTRY_PREFIX,
                directory_identity,
            )
            if retained_plan is not None:
                raise SyncError(
                    "pending cleanup retained entry requires manual cleanup: "
                    f"{entry.name}"
                )
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SyncError(
                    f"pending cleanup directory gained an entry: {entry.name}"
                ) from error
            active_plan = _pending_cleanup_internal_entry_plan(
                entry.name,
                PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                directory_identity,
            )
            _retain_pending_cleanup_entry(
                directory_fd,
                entry.name,
                directory_identity,
                active_plan or _pending_cleanup_entry_plan(metadata),
                label=f"pending cleanup directory gained an entry: {entry.name}",
            )
    if mutated:
        os.fsync(directory_fd)


def _remove_cleanup_ready_batch(
    home: Path,
    ticket: PendingBatchCleanupTicket,
) -> bool:
    if not _pending_link_pointer_is_absent(home):
        raise SyncError("refusing pending cleanup while an active pointer exists")
    current_ticket = _read_pending_cleanup_ticket(
        home,
        ticket.path,
        expected_ticket_identity=ticket.snapshot.file_identity,
    )
    if current_ticket is None or current_ticket != ticket:
        raise SyncError(
            f"pending cleanup ticket changed: {ticket.batch_root.name}"
        )
    quarantine_root = _personal_sync_root(home) / QUARANTINE_RELATIVE_PATH
    try:
        quarantine_fd = _open_directory_beneath(home, quarantine_root)
    except FileNotFoundError as error:
        raise SyncError("pending cleanup quarantine root is missing") from error
    batch_fd = -1
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        if not _bound_directory_matches(home, quarantine_root, quarantine_fd):
            raise SyncError("pending cleanup quarantine root changed")
        batch_name = ticket.batch_root.name
        isolated_name = _pending_cleanup_isolated_batch_name(batch_name)
        bound_batch_root = ticket.batch_root
        try:
            batch_fd = os.open(
                batch_name,
                directory_flags,
                dir_fd=quarantine_fd,
            )
        except FileNotFoundError:
            bound_batch_root = ticket.batch_root.with_name(isolated_name)
            try:
                batch_fd = os.open(
                    isolated_name,
                    directory_flags,
                    dir_fd=quarantine_fd,
                )
            except FileNotFoundError:
                _delete_pending_cleanup_ticket(home, ticket)
                return True
        if (
            _directory_identity(batch_fd) != ticket.batch_root_identity
            or not _bound_directory_matches(home, bound_batch_root, batch_fd)
        ):
            raise SyncError(
                f"pending cleanup batch root changed: {batch_name}"
            )
        _remove_pending_batch_directory_contents(
            batch_fd,
            ticket.batch_root_identity,
            _directory_mount_identity(batch_fd),
            [MAX_PENDING_CLEANUP_ENTRIES],
            depth=0,
        )
        with os.scandir(batch_fd) as iterator:
            if next(iterator, None) is not None:
                raise SyncError(
                    f"pending cleanup batch is not empty: {batch_name}"
                )
        if bound_batch_root == ticket.batch_root:
            _rename_noreplace_at(
                quarantine_fd,
                batch_name,
                quarantine_fd,
                isolated_name,
            )
            os.fsync(quarantine_fd)
            bound_batch_root = ticket.batch_root.with_name(isolated_name)
        current_root = os.stat(
            isolated_name,
            dir_fd=quarantine_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or (current_root.st_dev, current_root.st_ino)
            != ticket.batch_root_identity
            or _directory_identity(batch_fd) != ticket.batch_root_identity
            or not _bound_directory_matches(home, bound_batch_root, batch_fd)
        ):
            raise SyncError(
                f"pending cleanup batch root changed: {batch_name}"
            )
        os.rmdir(isolated_name, dir_fd=quarantine_fd)
        os.fsync(quarantine_fd)
        if _named_entry_identity(quarantine_fd, isolated_name) is not None:
            raise SyncError(
                f"pending cleanup batch root reappeared: {batch_name}"
            )
    finally:
        if batch_fd >= 0:
            _close_fd_quietly(batch_fd)
        _close_fd_quietly(quarantine_fd)
    _delete_pending_cleanup_ticket(home, ticket)
    return True


def _delete_pending_cleanup_ticket(
    home: Path,
    ticket: PendingBatchCleanupTicket,
) -> None:
    index_fd = _open_directory_beneath(home, ticket.path.parent)
    try:
        current = _read_managed_state_file_snapshot(
            home,
            ticket.path,
            index_fd,
            expected_identity=ticket.snapshot.file_identity,
        )
        if current != ticket.snapshot:
            raise SyncError(
                f"pending cleanup ticket changed: {ticket.batch_root.name}"
            )
        _isolate_and_delete_pending_cleanup_file(
            home,
            ticket.path,
            index_fd,
            ticket.snapshot,
            label=f"pending cleanup ticket {ticket.batch_root.name}",
        )
        remaining = _read_managed_state_file_snapshot(
            home,
            ticket.path,
            index_fd,
        )
        if remaining.exists:
            raise SyncError(
                f"pending cleanup ticket reappeared: {ticket.batch_root.name}"
            )
    finally:
        _close_fd_quietly(index_fd)


def _try_cleanup_committed_pending_batch(
    home: Path,
    batch: PendingLinkBatch,
) -> bool:
    if batch.pointer_snapshot is not None:
        print("warning: committed pending transaction still has an active pointer")
        return False
    try:
        ticket = _read_pending_cleanup_ticket(
            home,
            _pending_cleanup_ticket_path(home, batch.batch_root.name),
        )
        if ticket is None:
            raise SyncError("committed pending cleanup ticket is missing")
        if ticket.batch_root_identity != batch.batch_root_identity:
            raise SyncError("committed pending cleanup batch identity changed")
        return _remove_cleanup_ready_batch(home, ticket)
    except (OSError, SyncError) as error:
        print(
            "warning: committed pending transaction cleanup was deferred: "
            f"{batch.batch_root.name}: {error}"
        )
        return False


def _read_pending_cleanup_cursor(home: Path, index_root: Path) -> str | None:
    cursor_path = index_root / PENDING_CLEANUP_CURSOR_NAME
    try:
        index_fd = _open_directory_beneath(home, index_root)
        try:
            snapshot = _read_managed_state_file_snapshot(
                home,
                cursor_path,
                index_fd,
            )
        finally:
            _close_fd_quietly(index_fd)
    except (OSError, SyncError) as error:
        print(f"warning: ignoring invalid pending cleanup scan cursor: {error}")
        return None
    if not snapshot.exists:
        return None
    if snapshot.mode != 0o600 or snapshot.payload is None:
        print("warning: ignoring invalid pending cleanup scan cursor mode")
        return None
    try:
        cursor_name = snapshot.payload.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError as error:
        print(f"warning: ignoring invalid pending cleanup scan cursor: {error}")
        return None
    if snapshot.payload != (cursor_name + "\n").encode("ascii"):
        print("warning: ignoring non-canonical pending cleanup scan cursor")
        return None
    if not cursor_name.endswith(PENDING_CLEANUP_TICKET_SUFFIX):
        print("warning: ignoring invalid pending cleanup scan cursor ticket")
        return None
    batch_name = cursor_name[: -len(PENDING_CLEANUP_TICKET_SUFFIX)]
    if (
        len(batch_name) > MAX_PENDING_LINK_BATCH_NAME_BYTES
        or PENDING_LINK_BATCH_RE.fullmatch(batch_name) is None
    ):
        print("warning: ignoring invalid pending cleanup scan cursor batch")
        return None
    return cursor_name


def _write_pending_cleanup_cursor(
    home: Path,
    index_root: Path,
    ticket_name: str,
) -> None:
    cursor_path = index_root / PENDING_CLEANUP_CURSOR_NAME
    temp_path = index_root / PENDING_CLEANUP_CURSOR_TEMP_NAME
    payload = (ticket_name + "\n").encode("ascii")
    _discard_incomplete_pending_cleanup_ticket(home, temp_path)
    staged = _write_exclusive_internal_file(home, temp_path, payload)
    index_fd = _open_directory_beneath(home, index_root)
    try:
        current_temp = _read_managed_state_file_snapshot(
            home,
            temp_path,
            index_fd,
            expected_identity=staged.file_identity,
        )
        if current_temp != staged:
            raise SyncError("pending cleanup scan cursor temp changed")
        os.rename(
            temp_path.name,
            cursor_path.name,
            src_dir_fd=index_fd,
            dst_dir_fd=index_fd,
        )
        os.fsync(index_fd)
        published = _read_managed_state_file_snapshot(
            home,
            cursor_path,
            index_fd,
            expected_identity=staged.file_identity,
        )
        if published.payload != payload or published.mode != 0o600:
            raise SyncError("pending cleanup scan cursor changed during publication")
    finally:
        _close_fd_quietly(index_fd)


def _cleanup_ready_pending_batches(home: Path) -> int:
    if not _pending_link_pointer_is_absent(home):
        return 0
    index_root = _pending_cleanup_index_path(home)
    try:
        index_fd = _open_directory_beneath(home, index_root)
    except FileNotFoundError:
        return 0
    candidates: list[str] = []
    try:
        if not _bound_directory_matches(home, index_root, index_fd):
            raise SyncError("pending cleanup index changed")
        with os.scandir(index_fd) as iterator:
            for scanned, entry in enumerate(iterator, start=1):
                if scanned > MAX_PENDING_CLEANUP_BATCH_SCAN + 3:
                    print(
                        "warning: pending cleanup ticket scan reached its limit; "
                        "processing the bounded window"
                    )
                    break
                if not entry.name.endswith(PENDING_CLEANUP_TICKET_SUFFIX):
                    continue
                batch_name = entry.name[: -len(PENDING_CLEANUP_TICKET_SUFFIX)]
                if (
                    len(batch_name) <= MAX_PENDING_LINK_BATCH_NAME_BYTES
                    and PENDING_LINK_BATCH_RE.fullmatch(batch_name) is not None
                ):
                    candidates.append(entry.name)
    finally:
        _close_fd_quietly(index_fd)

    ordered = sorted(candidates)
    if not ordered:
        return 0
    cursor = _read_pending_cleanup_cursor(home, index_root)
    start = 0
    if cursor is not None:
        start = next(
            (index for index, name in enumerate(ordered) if name > cursor),
            0,
        )
    rotated = ordered[start:] + ordered[:start]
    selected = rotated[:MAX_PENDING_CLEANUP_BATCHES_PER_RUN]
    cleaned = 0
    for ticket_name in selected:
        batch_name = ticket_name[: -len(PENDING_CLEANUP_TICKET_SUFFIX)]
        try:
            ticket = _read_pending_cleanup_ticket(
                home,
                index_root / ticket_name,
            )
            if ticket is None:
                continue
            if _remove_cleanup_ready_batch(home, ticket):
                cleaned += 1
        except (FileNotFoundError, OSError, SyncError) as error:
            print(
                "warning: deferred pending transaction cleanup was retained: "
                f"{batch_name}: {error}"
            )
    _write_pending_cleanup_cursor(home, index_root, selected[-1])
    return cleaned


def _try_cleanup_ready_pending_batches(home: Path) -> int:
    try:
        return _cleanup_ready_pending_batches(home)
    except (OSError, SyncError) as error:
        print(f"warning: pending transaction cleanup scan was deferred: {error}")
        return 0


def _clear_pending_link_pointer(
    home: Path,
    batch: PendingLinkBatch,
    *,
    phase: str = "before",
) -> None:
    pointer_path = _pending_link_pointer_path(home)
    parent_fd = _open_directory_beneath(home, home)
    try:
        current = _read_managed_state_file_snapshot(home, pointer_path, parent_fd)
        if not current.exists:
            raise SyncError("pending link pointer disappeared and was not finalized")
        expected = batch.pointer_snapshot
        if (
            expected is None
            or not _managed_state_snapshot_exact(current, expected)
        ):
            raise SyncError("pending link pointer changed and was left in place")
        committed = _pending_commit_decision(home, batch)
        if phase == "before" and committed:
            raise SyncError(
                "refusing to clear committed pending transaction as precommit"
            )
        if phase == "after" and not committed:
            raise SyncError(
                "refusing to clear pending transaction without commit marker"
            )
        _verify_pending_link_phase(home, batch, phase)
        _quarantine_pending_link_pointer(
            home,
            batch,
            expected,
            parent_fd,
            label="complete",
        )
        batch.pointer_snapshot = None
    finally:
        _close_fd_quietly(parent_fd)


def _prepare_pending_managed_state_transaction(
    home: Path,
    batch: PendingLinkBatch,
    state: ManagedState,
) -> ManagedStateFileTransaction:
    if state != batch.state_after_value:
        raise SyncError("planned managed state changed before publication")
    transaction = _prepare_managed_state_transaction(home, state, batch.state_before)
    if (
        transaction.after.payload != batch.state_after.payload
        or transaction.after.mode != batch.state_after.mode
        or batch.state_after.file_identity is None
    ):
        raise SyncError("pending managed state evidence does not match planned state")
    transaction.batch_root = batch.batch_root
    transaction.before_evidence = (
        batch.batch_root / Path(*batch.state_before_evidence.parts)
        if batch.state_before_evidence is not None
        else None
    )
    transaction.after_evidence = (
        batch.batch_root / Path(*batch.state_after_evidence.parts)
    )
    transaction.after_evidence_identity = batch.state_after.file_identity
    return transaction


def _restore_pending_state_before(
    home: Path,
    batch: PendingLinkBatch,
) -> None:
    if not batch.state_before.exists:
        return
    if batch.state_before_evidence is None or batch.state_before.file_identity is None:
        raise SyncError("pending transaction state-before evidence is missing")
    evidence = batch.batch_root / Path(*batch.state_before_evidence.parts)
    evidence_parent_fd = _open_directory_beneath(home, evidence.parent)
    state_path = _state_path(home)
    state_parent_fd = _open_directory_beneath(home, state_path.parent)
    try:
        if _directory_identity(state_parent_fd) != batch.state_before.parent_identity:
            raise SyncError("pending transaction state parent changed")
        if _managed_state_name_exists(state_parent_fd, state_path.name):
            raise SyncError("refusing to overwrite canonical managed state during recovery")
        evidence_snapshot = _read_managed_state_file_snapshot(
            home,
            evidence,
            evidence_parent_fd,
            expected_identity=batch.state_before.file_identity,
        )
        if (
            evidence_snapshot.payload != batch.state_before.payload
            or evidence_snapshot.mode != batch.state_before.mode
        ):
            raise SyncError("pending transaction state-before evidence changed")
        os.link(
            evidence.name,
            state_path.name,
            src_dir_fd=evidence_parent_fd,
            dst_dir_fd=state_parent_fd,
            follow_symlinks=False,
        )
        os.fsync(state_parent_fd)
        restored = _read_managed_state_file_snapshot(
            home,
            state_path,
            state_parent_fd,
            expected_identity=batch.state_before.file_identity,
        )
        if (
            restored.file_identity != batch.state_before.file_identity
            or restored.payload != batch.state_before.payload
            or restored.mode != batch.state_before.mode
        ):
            raise SyncError("exact managed state restoration changed")
    finally:
        _close_fd_quietly(state_parent_fd)
        _close_fd_quietly(evidence_parent_fd)


def _rollback_pending_state_to_before(
    home: Path,
    batch: PendingLinkBatch,
    state_snapshot: ManagedStateFileSnapshot,
) -> tuple[ManagedState, ManagedStateFileSnapshot]:
    if _managed_state_snapshot_exact(state_snapshot, batch.state_before):
        return _load_managed_state_with_snapshot(home)
    if not state_snapshot.exists:
        _restore_pending_state_before(home, batch)
    elif _managed_state_snapshot_exact(state_snapshot, batch.state_after):
        state_path = _state_path(home)
        state_parent_fd = _open_directory_beneath(home, state_path.parent)
        try:
            if _directory_identity(state_parent_fd) != batch.state_after.parent_identity:
                raise SyncError("pending transaction state parent changed")
            transaction = ManagedStateFileTransaction(
                before=batch.state_after,
                after=ManagedStateFileSnapshot(exists=False),
                batch_root=batch.batch_root,
                state_parent_identity=batch.state_after.parent_identity,
            )
            moved, matches = _move_managed_state_entry_to_quarantine(
                home,
                transaction,
                state_parent_fd,
                state_path.name,
                "pending-precommit",
                expected_identity=batch.state_after.file_identity,
                expected_snapshot=batch.state_after,
            )
            if not matches:
                raise SyncError(
                    "pending precommit state changed and was retained in "
                    f"quarantine: {moved}"
                )
            if _managed_state_name_exists(state_parent_fd, state_path.name):
                raise SyncError(
                    "pending precommit canonical state reappeared during rollback"
                )
        finally:
            _close_fd_quietly(state_parent_fd)
        _restore_pending_state_before(home, batch)
    else:
        raise SyncError(
            "pending transaction found an ambiguous canonical managed state inode; "
            "the transaction and evidence were retained"
        )
    state, current_snapshot = _load_managed_state_with_snapshot(home)
    if (
        not _managed_state_snapshot_exact(current_snapshot, batch.state_before)
        or state != batch.state_before_value
    ):
        raise SyncError("pending precommit managed state restoration changed")
    return state, current_snapshot


def _pending_record_evidence_snapshot(
    home: Path,
    batch: PendingLinkBatch,
    record: PendingLinkRecord,
) -> SymlinkSnapshot:
    if (
        record.evidence is None
        or record.stage is None
        or record.link_target is None
        or record.evidence_identity is None
        or record.stage_identity is None
    ):
        raise SyncError(f"pending record has no produced evidence: {record.target}")
    evidence = batch.batch_root / Path(*record.evidence.parts)
    try:
        evidence_snapshot = _read_symlink_snapshot_beneath(home, evidence)
    except (FileNotFoundError, OSError, SyncError) as error:
        raise SyncError(
            f"pending link evidence is missing or unsafe: {record.target}"
        ) from error
    if (
        evidence_snapshot.link_target != record.link_target
        or evidence_snapshot.link_identity != record.evidence_identity
    ):
        raise SyncError(f"pending link evidence changed: {record.target}")
    stage = batch.batch_root / Path(*record.stage.parts)
    stage_snapshot = _read_optional_symlink_snapshot_beneath(home, stage)
    if stage_snapshot is None or (
        stage_snapshot.link_identity != record.stage_identity
        or stage_snapshot.link_identity != evidence_snapshot.link_identity
        or stage_snapshot.link_target != evidence_snapshot.link_target
    ):
        raise SyncError(f"pending link stage changed: {record.target}")
    return evidence_snapshot


def _pending_record_before_evidence_snapshot(
    home: Path,
    batch: PendingLinkBatch,
    record: PendingLinkRecord,
) -> SymlinkSnapshot:
    if (
        record.before_evidence is None
        or record.before_evidence_identity is None
        or record.planned_snapshot.link_target is None
    ):
        raise SyncError(f"pending record has no before evidence: {record.target}")
    snapshot = _read_symlink_snapshot_beneath(
        home,
        batch.batch_root / Path(*record.before_evidence.parts),
    )
    if (
        snapshot.link_identity != record.before_evidence_identity
        or snapshot.link_identity != record.planned_snapshot.link_identity
        or snapshot.link_target != record.planned_snapshot.link_target
    ):
        raise SyncError(f"pending before evidence changed: {record.target}")
    return snapshot


def _pending_record_backup_snapshot(
    home: Path,
    batch: PendingLinkBatch,
    record: PendingLinkRecord,
) -> SymlinkSnapshot | None:
    if record.backup is None:
        return None
    backup = batch.batch_root / Path(*record.backup.parts)
    snapshot = _read_optional_symlink_snapshot_beneath(home, backup)
    if snapshot is None:
        if _path_exists_or_is_link(backup):
            raise SyncError(f"pending backup is not a symlink: {record.target}")
        return None
    if (
        snapshot.link_identity != record.before_evidence_identity
        or snapshot.link_target != record.planned_snapshot.link_target
    ):
        raise SyncError(f"pending backup changed: {record.target}")
    return snapshot


def _pending_target_snapshot(
    home: Path,
    target: Path,
) -> tuple[SymlinkSnapshot | None, bool]:
    snapshot = _read_optional_symlink_snapshot_beneath(home, target)
    return snapshot, _path_exists_or_is_link(target)


def _symlink_snapshot_matches(
    actual: SymlinkSnapshot | None,
    expected_parent_identity: tuple[int, int] | None,
    expected_identity: tuple[int, int] | None,
    expected_target: str | None,
) -> bool:
    return (
        actual is not None
        and expected_parent_identity is not None
        and expected_identity is not None
        and expected_target is not None
        and actual.parent_identity == expected_parent_identity
        and actual.link_identity == expected_identity
        and actual.link_target == expected_target
    )


def _symlink_snapshot_leaf_matches(
    actual: SymlinkSnapshot | None,
    expected_identity: tuple[int, int] | None,
    expected_target: str | None,
) -> bool:
    return (
        actual is not None
        and expected_identity is not None
        and expected_target is not None
        and actual.link_identity == expected_identity
        and actual.link_target == expected_target
    )


def _restore_pending_record_before(
    home: Path,
    batch: PendingLinkBatch,
    record: PendingLinkRecord,
    before_evidence: SymlinkSnapshot,
) -> None:
    assert record.before_evidence is not None
    target = home / Path(*record.target.parts)
    expected_target = ReconcileTargetSnapshot(
        parent_identity=record.planned_snapshot.parent_identity,
        ancestor_identity=record.planned_snapshot.parent_identity,
    )
    restored = _publish_symlink_hardlink_beneath(
        home,
        batch.batch_root / Path(*record.before_evidence.parts),
        target,
        before_evidence,
        expected_target,
        {},
    )
    if (
        restored.link_identity != record.planned_snapshot.link_identity
        or restored.link_target != record.planned_snapshot.link_target
    ):
        raise SyncError(f"pending preimage restoration changed: {record.target}")


def _managed_state_snapshot_exact(
    actual: ManagedStateFileSnapshot,
    expected: ManagedStateFileSnapshot,
) -> bool:
    if actual.exists != expected.exists:
        return False
    if actual.parent_identity != expected.parent_identity:
        return False
    if not expected.exists:
        return True
    return (
        actual.file_identity == expected.file_identity
        and actual.mode == expected.mode
        and actual.payload == expected.payload
    )


def _managed_state_staging_snapshot_transition_is_allowed(
    initial: ManagedStateFileSnapshot,
    current: ManagedStateFileSnapshot,
    bound_parent_identity: tuple[int, int],
) -> bool:
    if current.parent_identity != bound_parent_identity:
        return False
    if initial.exists or initial.parent_identity is not None:
        return _managed_state_snapshot_exact(current, initial)
    return not current.exists


def _bind_managed_state_parent_for_pending_staging(
    home: Path,
    initial_snapshot: ManagedStateFileSnapshot,
    expected_state: ManagedState,
) -> ManagedStateFileSnapshot:
    state_parent = _state_path(home).parent
    state_parent_fd = _open_or_create_directory_beneath(
        home,
        state_parent,
        mode=0o700,
    )
    try:
        bound_parent_identity = _directory_identity(state_parent_fd)
        if not _bound_directory_matches(home, state_parent, state_parent_fd):
            raise SyncError(
                "managed state parent changed before pending transaction staging"
            )
        current_state, current_snapshot = _load_managed_state_with_snapshot(home)
        if (
            not _bound_directory_matches(home, state_parent, state_parent_fd)
            or not _managed_state_staging_snapshot_transition_is_allowed(
                initial_snapshot,
                current_snapshot,
                bound_parent_identity,
            )
        ):
            raise SyncError(
                "managed state snapshot changed before pending transaction staging"
            )
    finally:
        _close_fd_quietly(state_parent_fd)
    if current_state != expected_state:
        raise SyncError("managed state changed before pending transaction staging")
    return current_snapshot


def _verify_pending_link_claims(
    home: Path,
    batch: PendingLinkBatch,
    phase: str,
) -> None:
    if phase == "before":
        claims = batch.claims_before
    elif phase == "after":
        claims = batch.claims_after
    else:
        raise SyncError(f"unsupported pending claim phase: {phase}")
    for claim in claims:
        evidence_snapshot = _read_symlink_snapshot_beneath(
            home,
            batch.batch_root / Path(*claim.evidence.parts),
        )
        if (
            evidence_snapshot.link_identity != claim.link_identity
            or evidence_snapshot.link_target != claim.link_target
        ):
            raise SyncError(
                f"pending {phase}-state claim evidence changed: {claim.target}"
            )
        target = home / Path(*claim.target.parts)
        target_snapshot, _target_exists = _pending_target_snapshot(home, target)
        if _symlink_snapshot_matches(
            target_snapshot,
            claim.parent_identity,
            claim.link_identity,
            claim.link_target,
        ):
            continue
        if _symlink_snapshot_leaf_matches(
            target_snapshot,
            claim.link_identity,
            claim.link_target,
        ):
            raise SyncError(
                f"pending {phase}-state claim parent changed: {claim.target}"
            )
        raise SyncError(
            f"pending {phase}-state claim is not the exact evidence inode: "
            f"{claim.target}"
        )


def _verify_pending_before_absences(
    home: Path,
    batch: PendingLinkBatch,
) -> None:
    before_semantic_keys = {
        (semantic[0], semantic[1])
        for semantic in _pending_state_claim_semantics(
            home,
            batch.state_before_value,
        )
    }
    for record in batch.records:
        key = (record.scope, record.target)
        if record.action == "create":
            _verify_pending_record_bound_create_absence(
                home,
                record,
                phase="before-state",
            )
        elif record.action == "retire-absent" and key in before_semantic_keys:
            _verify_pending_record_bound_retired_absence(
                home,
                record,
                phase="before-state",
            )


def _verify_pending_release_expectations(
    home: Path,
    expectations: tuple[PendingReleaseExpectation, ...],
    *,
    phase: str,
) -> None:
    for expectation in expectations:
        identity, directory_identity = (
            _installed_release_identity_and_directory_identity(
                home,
                expectation.owner,
                expectation.sha,
            )
        )
        if (
            directory_identity != expectation.directory_identity
            or identity[2] != expectation.tree_sha256
        ):
            raise SyncError(
                f"pending {phase}-state release tree changed: "
                f"{expectation.owner}@{expectation.sha}"
            )


def _verify_pending_link_phase(
    home: Path,
    batch: PendingLinkBatch,
    phase: str,
) -> None:
    if phase == "before":
        expected_snapshot = batch.state_before
        expected_state = batch.state_before_value
    elif phase == "after":
        expected_snapshot = batch.state_after
        expected_state = batch.state_after_value
    else:
        raise SyncError(f"unsupported pending transaction phase: {phase}")
    state, state_snapshot = _load_managed_state_with_snapshot(home)
    if not _managed_state_snapshot_exact(state_snapshot, expected_snapshot):
        raise SyncError(
            f"pending {phase}-state canonical managed state is not exact"
        )
    if state != expected_state:
        raise SyncError(f"pending {phase}-state managed payload changed")
    expectations = (
        batch.releases_before if phase == "before" else batch.releases_after
    )
    _verify_pending_release_expectations(
        home,
        expectations,
        phase=phase,
    )
    _verify_pending_link_claims(home, batch, phase)
    if phase == "before":
        _verify_pending_before_absences(home, batch)


def _verify_committed_pending_link_records(
    home: Path,
    batch: PendingLinkBatch,
) -> None:
    for record in batch.records:
        target = home / Path(*record.target.parts)
        target_snapshot, target_exists = _pending_target_snapshot(home, target)
        producing = record.action in {
            "create",
            "replace",
            "quarantine-replace",
        }
        destructive = record.action in {
            "replace",
            "quarantine-replace",
            "remove",
            "quarantine-remove",
        }
        if destructive and _pending_record_backup_snapshot(home, batch, record) is None:
            raise SyncError(f"committed pending backup is missing: {record.target}")
        if producing:
            evidence = _pending_record_evidence_snapshot(home, batch, record)
            target_matches = _symlink_snapshot_matches(
                target_snapshot,
                record.planned_snapshot.parent_identity,
                evidence.link_identity,
                evidence.link_target,
            )
            if not target_matches and _symlink_snapshot_leaf_matches(
                target_snapshot,
                evidence.link_identity,
                evidence.link_target,
            ):
                raise SyncError(
                    f"committed pending target parent changed: {record.target}"
                )
            if not target_matches:
                raise SyncError(
                    "committed pending target is not the exact evidence inode: "
                    f"{record.target}"
                )
            continue
        if record.action == "retire-absent":
            # The committed state deliberately relinquishes this already-missing
            # path. A later foreign occupant is outside the synchronizer's claim.
            continue
        if record.scope == "current":
            if target_exists:
                raise SyncError(
                    f"committed current removal target still exists: {record.target}"
                )
            continue
        before = _pending_record_before_evidence_snapshot(home, batch, record)
        before_leaf_matches = _symlink_snapshot_leaf_matches(
            target_snapshot,
            before.link_identity,
            before.link_target,
        )
        if _symlink_snapshot_matches(
            target_snapshot,
            record.planned_snapshot.parent_identity,
            before.link_identity,
            before.link_target,
        ):
            raise SyncError(
                f"committed managed removal did not run: {record.target}"
            )
        if before_leaf_matches:
            raise SyncError(
                "committed managed removal target parent changed: "
                f"{record.target}"
            )
        # A foreign replacement is unclaimed by the committed state and is
        # intentionally preserved.


def _recover_pending_link_transaction(
    home: Path,
    state: ManagedState,
    state_snapshot: ManagedStateFileSnapshot,
    *,
    dry_run: bool,
) -> tuple[ManagedState, ManagedStateFileSnapshot, bool]:
    batch = _load_pending_link_batch(home)
    if batch is None:
        return state, state_snapshot, False
    committed = _pending_commit_decision(home, batch)
    if dry_run:
        # Validate the pointer without mutation and let the caller report that
        # recovery must precede any requested new work.
        return state, state_snapshot, True

    if committed:
        if (
            not _managed_state_snapshot_exact(state_snapshot, batch.state_after)
            or state != batch.state_after_value
        ):
            raise SyncError(
                "committed pending managed state is missing or changed; the "
                "transaction and evidence were retained"
            )
        _verify_committed_pending_link_records(home, batch)
        _mark_pending_batch_cleanup_ready(home, batch)
        _clear_pending_link_pointer(home, batch, phase="after")
        _try_cleanup_committed_pending_batch(home, batch)
        return state, state_snapshot, True

    state, state_snapshot = _rollback_pending_state_to_before(
        home,
        batch,
        state_snapshot,
    )
    for record in reversed(batch.records):
        target = home / Path(*record.target.parts)
        target_snapshot, target_exists = _pending_target_snapshot(home, target)
        producing = record.action in {
            "create",
            "replace",
            "quarantine-replace",
        }
        destructive = record.action in {
            "replace",
            "quarantine-replace",
            "remove",
            "quarantine-remove",
        }
        produced_evidence = (
            _pending_record_evidence_snapshot(home, batch, record)
            if producing
            else None
        )
        before_evidence = (
            _pending_record_before_evidence_snapshot(home, batch, record)
            if destructive
            else None
        )
        if destructive:
            _pending_record_backup_snapshot(home, batch, record)
        target_has_produced_leaf = (
            produced_evidence is not None
            and _symlink_snapshot_leaf_matches(
                target_snapshot,
                produced_evidence.link_identity,
                produced_evidence.link_target,
            )
        )
        target_is_produced = (
            produced_evidence is not None
            and _symlink_snapshot_matches(
                target_snapshot,
                record.planned_snapshot.parent_identity,
                produced_evidence.link_identity,
                produced_evidence.link_target,
            )
        )
        target_is_before = before_evidence is not None and _symlink_snapshot_matches(
            target_snapshot,
            record.planned_snapshot.parent_identity,
            before_evidence.link_identity,
            before_evidence.link_target,
        )
        if target_has_produced_leaf and not target_is_produced:
            raise SyncError(
                f"pending produced target parent changed: {record.target}"
            )
        if target_is_produced:
            assert target_snapshot is not None
            assert produced_evidence is not None
            _remove_expected_symlink_beneath(
                home,
                target,
                produced_evidence.link_target,
                target_snapshot,
            )
            target_snapshot = None
            target_exists = False
        if destructive:
            assert before_evidence is not None
            if target_is_before:
                continue
            if target_exists:
                raise SyncError(
                    "pending rollback found a foreign target; evidence and pointer "
                    f"were retained: {record.target}"
                )
            _restore_pending_record_before(home, batch, record, before_evidence)
            continue
        if target_is_produced or not target_exists:
            continue
        if record.scope == "managed" and record.action == "create":
            # The before-state never owned this path. Preserve a foreign occupant
            # without adopting it into managed state.
            continue
        if record.action == "retire-absent":
            raise SyncError(
                "pending retired absence found a foreign target; evidence and "
                f"pointer were retained: {record.target}"
            )
        raise SyncError(
            "pending current creation found a foreign target; evidence and pointer "
            f"were retained: {record.target}"
        )

    _clear_pending_link_pointer(home, batch, phase="before")
    state, state_snapshot = _load_managed_state_with_snapshot(home)
    return state, state_snapshot, True


def _order_destructive_reconcile_actions(
    home: Path,
    actions: list[ReconcileAction],
    required_replacements: dict[Path, list[LinkEntry]],
) -> list[ReconcileAction]:
    producers = {
        action.target: action
        for action in actions
        if action.action in {"replace", "quarantine-replace"}
    }
    dependencies: dict[ReconcileAction, set[ReconcileAction]] = {
        action: set() for action in actions
    }
    order = {action: index for index, action in enumerate(actions)}
    for action in actions:
        for entry in required_replacements.get(action.target, []):
            replacement_target = _entry_target_path(home, entry)
            if replacement_target == action.target:
                if (
                    action.action not in {"replace", "quarantine-replace"}
                    or action.link_target != _desired_link_target(home, entry)
                ):
                    raise SyncError(
                        "same-path replacement requires a matching replace action: "
                        f"{action.target}"
                    )
                continue
            producer = producers.get(replacement_target)
            if producer is not None:
                dependencies[action].add(producer)

    sorter = TopologicalSorter(dependencies)
    try:
        sorter.prepare()
    except CycleError as error:
        cycle_targets = (
            error.args[1]
            if len(error.args) > 1 and isinstance(error.args[1], list)
            else []
        )
        cycle = " -> ".join(str(action.target) for action in cycle_targets)
        detail = f": {cycle}" if cycle else ""
        raise SyncError(
            f"replacement action dependency cycle detected{detail}"
        ) from None

    ordered: list[ReconcileAction] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready(), key=order.__getitem__)
        ordered.extend(ready)
        sorter.done(*ready)
    return ordered


def _ordered_reconcile_actions(
    home: Path,
    actions: list[ReconcileAction],
    required_replacements: dict[Path, list[LinkEntry]] | None = None,
) -> list[ReconcileAction]:
    create_actions = [action for action in actions if action.action == "create"]
    destructive_actions = _order_destructive_reconcile_actions(
        home,
        [
            action
            for action in actions
            if action.action
            in {"replace", "quarantine-replace", "remove", "quarantine-remove"}
        ],
        required_replacements or {},
    )
    ordered = create_actions + destructive_actions
    if len(ordered) != len(actions):
        unknown = sorted(
            {action.action for action in actions}
            - {
                "create",
                "replace",
                "quarantine-replace",
                "remove",
                "quarantine-remove",
            }
        )
        raise SyncError(f"unknown reconciliation action(s): {', '.join(unknown)}")
    return ordered


def _apply_reconcile_actions(
    home: Path,
    actions: list[ReconcileAction],
    *,
    dry_run: bool,
    required_replacements: dict[Path, list[LinkEntry]] | None = None,
    pending_batch: PendingLinkBatch | None = None,
    pending_scope: str | None = None,
    batch_root: Path | None = None,
    transaction: ReconcileTransaction | None = None,
) -> ReconcileTransaction | None:
    ordered_actions = _ordered_reconcile_actions(
        home,
        actions,
        required_replacements,
    )

    if dry_run:
        if transaction is not None:
            raise SyncError("dry-run reconciliation cannot mutate a transaction")
        for action in ordered_actions:
            if action.action.endswith("remove"):
                print(f"would {action.action} symlink {action.target}")
            else:
                print(
                    f"would {action.action} symlink {action.target} -> {action.link_target}"
                )
        return None

    missing_snapshots = [
        str(action.target)
        for action in ordered_actions
        if action.planned_snapshot is None
    ]
    if missing_snapshots:
        raise SyncError(
            "reconciliation action has no planning snapshot: "
            + ", ".join(missing_snapshots)
        )

    pending_records: dict[PurePosixPath, PendingLinkRecord] = {}
    if pending_batch is not None:
        if pending_scope not in {"current", "managed"}:
            raise SyncError("pending reconciliation requires an explicit scope")
        if batch_root is not None and batch_root != pending_batch.batch_root:
            raise SyncError("reconciliation received conflicting quarantine batches")
        batch_root = pending_batch.batch_root
        expected_records = [
            record
            for record in pending_batch.records
            if record.scope == pending_scope and record.action != "retire-absent"
        ]
        if len(expected_records) != len(ordered_actions):
            raise SyncError(f"pending {pending_scope} action count changed")
        pending_records = {record.target: record for record in expected_records}
        effective_actions: list[ReconcileAction] = []
        for record, action in zip(expected_records, ordered_actions):
            try:
                relative_target = PurePosixPath(*action.target.relative_to(home).parts)
            except ValueError as error:
                raise SyncError(f"pending target is outside home: {action.target}") from error
            original_snapshot = action.planned_snapshot
            if record.planned_snapshot != original_snapshot:
                parent_refresh_is_valid = (
                    action.action == "create"
                    and original_snapshot is not None
                    and original_snapshot.parent_identity is None
                    and original_snapshot.link_identity is None
                    and original_snapshot.link_target is None
                    and bool(original_snapshot.missing_parent_parts)
                    and record.planned_snapshot.parent_identity is not None
                    and record.planned_snapshot.link_identity is None
                    and record.planned_snapshot.link_target is None
                    and record.planned_snapshot.ancestor_identity
                    == record.planned_snapshot.parent_identity
                    and not record.planned_snapshot.missing_parent_parts
                )
                if not parent_refresh_is_valid:
                    raise SyncError(
                        f"pending {pending_scope} planning snapshot changed: "
                        f"{action.target}"
                    )
                action = replace(
                    action,
                    planned_snapshot=record.planned_snapshot,
                )
            if (
                record.target != relative_target
                or record.action != action.action
                or record.kind != action.kind
                or record.planned_snapshot != action.planned_snapshot
                or record.link_target
                != (
                    action.link_target
                    if action.action in {"create", "replace", "quarantine-replace"}
                    else None
                )
            ):
                raise SyncError(f"pending {pending_scope} action changed: {action.target}")
            effective_actions.append(action)
        ordered_actions = effective_actions
    create_actions = [action for action in ordered_actions if action.action == "create"]
    destructive_actions = [
        action for action in ordered_actions if action.action != "create"
    ]
    if transaction is None:
        transaction = ReconcileTransaction(batch_root=batch_root, mutations=[])
    else:
        if transaction.mutations:
            raise SyncError("reconciliation transaction is not empty before apply")
        if (
            transaction.batch_root is not None
            and batch_root is not None
            and transaction.batch_root != batch_root
        ):
            raise SyncError("reconciliation transaction has a conflicting batch")
        if transaction.batch_root is None:
            transaction.batch_root = batch_root
    created_parent_identities: dict[Path, tuple[int, int]] = {}
    try:
        for action in create_actions:
            pending_record: PendingLinkRecord | None = None
            if pending_batch is not None:
                try:
                    relative_target = action.target.relative_to(home)
                except ValueError as error:
                    raise SyncError(
                        f"managed target is outside home: {action.target}"
                    ) from error
                pending_record = pending_records.get(PurePosixPath(*relative_target.parts))
                if pending_record is None:
                    raise SyncError(
                        f"pending create evidence is missing: {action.target}"
                    )
                if (
                    pending_record.link_target != action.link_target
                    or pending_record.kind != action.kind
                ):
                    raise SyncError(
                        f"pending create evidence changed: {action.target}"
                    )
            if pending_record is None:
                created_snapshot = _create_symlink_beneath(
                    home,
                    action.target,
                    action.link_target,
                    action.kind,
                    action.planned_snapshot,
                    created_parent_identities,
                )
            else:
                assert pending_record.stage is not None
                stage = pending_batch.batch_root / Path(*pending_record.stage.parts)
                stage_snapshot = _read_symlink_snapshot_beneath(home, stage)
                evidence_snapshot = _pending_record_evidence_snapshot(
                    home,
                    pending_batch,
                    pending_record,
                )
                if (
                    stage_snapshot.link_identity != evidence_snapshot.link_identity
                    or stage_snapshot.link_target != evidence_snapshot.link_target
                ):
                    raise SyncError(
                        f"pending create stage is not inode-bound: {action.target}"
                    )
                assert action.planned_snapshot is not None
                created_snapshot = _publish_symlink_hardlink_beneath(
                    home,
                    stage,
                    action.target,
                    stage_snapshot,
                    action.planned_snapshot,
                    created_parent_identities,
                )
            transaction.mutations.append(
                ReconcileMutation(
                    action=action,
                    created_snapshot=created_snapshot,
                )
            )
            _verify_created_reconcile_mutation(home, transaction.mutations[-1])
            print(f"created symlink {action.target} -> {action.link_target}")
        _verify_reconcile_action_targets(home, create_actions)

        if destructive_actions and transaction.batch_root is None:
            transaction.batch_root = _quarantine_batch_root(
                home,
                destructive_actions,
            )
        for action in destructive_actions:
            assert transaction.batch_root is not None
            _ensure_safe_target_parent(home, action.target)
            replacements = (required_replacements or {}).get(action.target, [])
            prior_replacements = [
                entry
                for entry in replacements
                if _entry_target_path(home, entry) != action.target
            ]
            if prior_replacements:
                _verify_required_replacement_targets(home, prior_replacements)
            try:
                relative_target = action.target.relative_to(home)
            except ValueError as error:
                raise SyncError(
                    f"quarantine target is outside home: {action.target}"
                ) from error
            pending_record = pending_records.get(
                PurePosixPath(*relative_target.parts)
            )
            if pending_batch is not None:
                if pending_record is None or pending_record.backup is None:
                    raise SyncError(
                        f"pending destructive evidence is missing: {action.target}"
                    )
                _pending_record_before_evidence_snapshot(
                    home,
                    pending_batch,
                    pending_record,
                )
                backup = pending_batch.batch_root / Path(*pending_record.backup.parts)
            else:
                backup = transaction.batch_root / "links" / relative_target
            backup_parent_fd = _open_or_create_directory_beneath(
                home,
                backup.parent,
            )
            backup_parent_identity = _directory_identity(backup_parent_fd)
            _close_fd_quietly(backup_parent_fd)
            assert action.planned_snapshot is not None
            _atomic_move_beneath_home(
                home,
                action.target,
                backup,
                action.planned_snapshot,
                backup_parent_identity,
            )
            mutation = ReconcileMutation(action=action, backup=backup)
            transaction.mutations.append(mutation)
            _verify_reconcile_backup(home, action, backup)
            if prior_replacements:
                _verify_required_replacement_targets(home, prior_replacements)
            print(f"quarantined symlink {action.target} -> {backup}")

            if action.action in {"replace", "quarantine-replace"}:
                replacement_plan = ReconcileTargetSnapshot(
                    parent_identity=action.planned_snapshot.parent_identity,
                    ancestor_identity=action.planned_snapshot.parent_identity,
                )
                if pending_record is None:
                    mutation.created_snapshot = _create_symlink_beneath(
                        home,
                        action.target,
                        action.link_target,
                        action.kind,
                        replacement_plan,
                        created_parent_identities,
                    )
                else:
                    assert pending_record.stage is not None
                    stage = pending_batch.batch_root / Path(*pending_record.stage.parts)
                    stage_snapshot = _read_symlink_snapshot_beneath(home, stage)
                    evidence_snapshot = _pending_record_evidence_snapshot(
                        home,
                        pending_batch,
                        pending_record,
                    )
                    if (
                        stage_snapshot.link_identity != evidence_snapshot.link_identity
                        or stage_snapshot.link_target != evidence_snapshot.link_target
                    ):
                        raise SyncError(
                            f"pending replacement stage changed: {action.target}"
                        )
                    mutation.created_snapshot = _publish_symlink_hardlink_beneath(
                        home,
                        stage,
                        action.target,
                        stage_snapshot,
                        replacement_plan,
                        created_parent_identities,
                    )
                _verify_created_reconcile_mutation(home, mutation)
                _verify_reconcile_action_targets(home, [action])
                if replacements:
                    _verify_required_replacement_targets(home, replacements)
                print(f"replaced symlink {action.target} -> {action.link_target}")
            else:
                print(f"removed stale symlink {action.target}")
    except BaseException as error:
        try:
            _rollback_reconcile_transaction(home, transaction)
        except SyncError as rollback_error:
            raise SyncError(
                "reconciliation failed and link rollback was incomplete: "
                f"{rollback_error}"
            ) from error
        transaction.mutations.clear()
        raise _SafeReconcileApplyError(f"reconciliation failed: {error}") from error

    return transaction


def _verify_reconcile_backup(
    home: Path,
    action: ReconcileAction,
    backup: Path,
) -> None:
    if action.expected_link_target is None or action.planned_snapshot is None:
        raise SyncError(f"destructive action has no expected symlink: {action.target}")
    if (
        action.planned_snapshot.link_identity is None
        or action.planned_snapshot.link_target is None
    ):
        raise SyncError(f"destructive action planned an absent target: {action.target}")
    try:
        backup_snapshot = _read_symlink_snapshot_beneath(home, backup)
    except (FileNotFoundError, OSError, SyncError) as error:
        raise SyncError(f"reconciliation backup is missing: {backup}") from error
    if (
        backup_snapshot.link_identity != action.planned_snapshot.link_identity
        or backup_snapshot.link_target != action.planned_snapshot.link_target
        or backup_snapshot.link_target != action.expected_link_target
    ):
        raise SyncError(f"target changed after preflight: {action.target}")


def _rollback_reconcile_transaction(
    home: Path,
    transaction: ReconcileTransaction | None,
) -> None:
    if transaction is None:
        return
    errors: list[str] = []
    for mutation in reversed(transaction.mutations):
        action = mutation.action
        try:
            if _path_exists_or_is_link(action.target):
                if mutation.created_snapshot is None:
                    raise SyncError(
                        f"changed target was left in place during rollback: {action.target}"
                    )
                _remove_expected_symlink_beneath(
                    home,
                    action.target,
                    action.link_target,
                    mutation.created_snapshot,
                )
            if mutation.backup is None:
                continue

            backup = mutation.backup
            try:
                _verify_reconcile_backup(home, action, backup)
            except SyncError as backup_error:
                raise SyncError(
                    "reconciliation backup changed and was retained in quarantine: "
                    f"{action.target} -> {backup}"
                ) from backup_error
            restore_snapshot = _capture_reconcile_target_snapshot(
                home,
                action.target,
            )
            if restore_snapshot.link_identity is not None:
                raise SyncError(
                    f"refusing to overwrite changed target during rollback: {action.target}"
                )
            if (
                action.planned_snapshot is None
                or restore_snapshot.parent_identity
                != action.planned_snapshot.parent_identity
            ):
                raise SyncError(
                    "refusing to restore through changed target parent during "
                    f"rollback: {action.target.parent}"
                )
            assert action.expected_link_target is not None
            backup_snapshot = _read_symlink_snapshot_beneath(home, backup)
            _atomic_move_beneath_home(
                home,
                backup,
                action.target,
                ReconcileTargetSnapshot(
                    parent_identity=backup_snapshot.parent_identity,
                    link_identity=action.planned_snapshot.link_identity,
                    link_target=action.planned_snapshot.link_target,
                    ancestor_identity=backup_snapshot.parent_identity,
                ),
                action.planned_snapshot.parent_identity,
            )
            restored_snapshot = _read_symlink_snapshot_beneath(home, action.target)
            if (
                restored_snapshot.parent_identity
                != action.planned_snapshot.parent_identity
                or restored_snapshot.link_identity
                != action.planned_snapshot.link_identity
                or restored_snapshot.link_target
                != action.planned_snapshot.link_target
            ):
                raise SyncError(
                    f"restored symlink changed during rollback: {action.target}"
                )
        except (OSError, SyncError) as error:
            errors.append(f"{action.target}: {error}")

    if errors:
        raise SyncError("; ".join(errors))


def _commit_reconcile_transaction(
    transaction: ReconcileTransaction | None,
) -> None:
    """Leave evidence cleanup to the committed pending-batch ticket flow."""


def _verify_created_reconcile_mutation(
    home: Path,
    mutation: ReconcileMutation,
) -> None:
    if mutation.created_snapshot is None:
        raise SyncError(
            f"reconciliation mutation has no created snapshot: {mutation.action.target}"
        )
    try:
        actual_snapshot = _read_symlink_snapshot_beneath(
            home,
            mutation.action.target,
        )
    except (FileNotFoundError, OSError, SyncError) as error:
        raise SyncError(
            f"created symlink changed during reconciliation: {mutation.action.target}"
        ) from error
    if actual_snapshot != mutation.created_snapshot:
        raise SyncError(
            f"created symlink changed during reconciliation: {mutation.action.target}"
        )


def _verify_reconcile_action_targets(
    home: Path,
    actions: list[ReconcileAction],
) -> None:
    for action in actions:
        try:
            actual_target = _read_symlink_beneath(home, action.target)
        except (FileNotFoundError, OSError, SyncError) as error:
            raise SyncError(
                f"reconciled symlink verification failed: {action.target}"
            ) from error
        if actual_target != action.link_target:
            raise SyncError(f"reconciled symlink verification failed: {action.target}")


def _verify_required_replacement_targets(
    home: Path,
    required_replacements: list[LinkEntry],
) -> None:
    for entry in required_replacements:
        target = _entry_target_path(home, entry)
        expected = _desired_link_target(home, entry)
        try:
            actual = _read_symlink_beneath(home, target)
        except (FileNotFoundError, OSError, SyncError) as error:
            raise SyncError(
                f"active replacement target changed before removal: {target}"
            ) from error
        if actual != expected:
            raise SyncError(
                f"active replacement target changed before removal: {target}"
            )


def _verify_desired_entries(home: Path, desired_entries: list[LinkEntry]) -> None:
    issues: list[str] = []
    for entry in desired_entries:
        target = _entry_target_path(home, entry)
        desired = _desired_link_target(home, entry)
        actual = _read_optional_symlink_target_beneath(home, target)
        if actual == desired:
            continue
        if _is_optional_desired_entry(entry):
            continue
        if actual is None:
            issues.append(f"missing managed symlink: {target}")
        else:
            issues.append(f"managed symlink drift: {target}")
    if issues:
        for issue in issues:
            print(f"sync issue: {issue}")
        raise SyncError(f"managed link verification failed with {len(issues)} issue(s)")


def _committed_state(
    home: Path,
    desired_entries: list[LinkEntry],
    owner_shas: dict[str, str],
    managed_targets: set[PurePosixPath] | None = None,
) -> ManagedState:
    links: dict[PurePosixPath, ManagedLinkRecord] = {}
    for entry in desired_entries:
        sha = owner_shas.get(entry.owner)
        if sha is None:
            raise SyncError(f"committed release is missing for owner {entry.owner}")
        if managed_targets is not None and entry.target not in managed_targets:
            continue
        target = _entry_target_path(home, entry)
        desired = _desired_link_target(home, entry)
        actual = _read_optional_symlink_target_beneath(home, target)
        if actual != desired:
            if _is_optional_desired_entry(entry):
                continue
            issue = "missing" if actual is None else "drifted"
            raise SyncError(
                f"mandatory desired link {issue} before state commit: {target}"
            )
        links[entry.target] = ManagedLinkRecord(
            source=entry.source,
            target=entry.target,
            kind=entry.kind,
            owner=entry.owner,
            link_target=desired,
            release_sha=sha,
        )
    return ManagedState(owners=dict(owner_shas), links=links)


def _planned_committed_state(
    home: Path,
    desired_entries: list[LinkEntry],
    owner_shas: dict[str, str],
    managed_targets: set[PurePosixPath],
) -> ManagedState:
    links: dict[PurePosixPath, ManagedLinkRecord] = {}
    for entry in desired_entries:
        if entry.target not in managed_targets:
            continue
        sha = owner_shas.get(entry.owner)
        if sha is None:
            raise SyncError(f"planned release is missing for owner {entry.owner}")
        links[entry.target] = ManagedLinkRecord(
            source=entry.source,
            target=entry.target,
            kind=entry.kind,
            owner=entry.owner,
            link_target=_desired_link_target(home, entry),
            release_sha=_validate_release_sha(sha),
        )
    return ManagedState(owners=dict(owner_shas), links=links)


def _verify_published_state_matches_desired(
    home: Path,
    desired_entries: list[LinkEntry],
    expected_state: ManagedState,
) -> None:
    published_state = _load_managed_state(home)
    if published_state != expected_state:
        raise SyncError("managed link state changed after publication")
    observed_state = _committed_state(
        home,
        desired_entries,
        expected_state.owners,
        set(expected_state.links),
    )
    if observed_state != published_state:
        raise SyncError(
            "desired links no longer match managed link state after publication"
        )


def _verify_published_state_transaction(
    home: Path,
    transaction: ManagedStateFileTransaction | None,
) -> None:
    if (
        transaction is None
        or not transaction.published
        or transaction.state_parent_identity is None
        or transaction.published_identity is None
    ):
        raise SyncError("managed state publication is not bound to its transaction")
    if not _canonical_managed_state_matches(
        home,
        _state_path(home),
        transaction.after,
        transaction.state_parent_identity,
        transaction.published_identity,
    ):
        raise SyncError("published managed state changed after release validation")


def _capture_managed_state_link_snapshots(
    home: Path,
    state: ManagedState,
) -> dict[PurePosixPath, SymlinkSnapshot]:
    snapshots: dict[PurePosixPath, SymlinkSnapshot] = {}
    for relative_target, record in sorted(
        state.links.items(),
        key=lambda item: item[0].as_posix(),
    ):
        target = home / Path(*relative_target.parts)
        snapshot = _read_optional_symlink_snapshot_beneath(home, target)
        if snapshot is None:
            continue
        if snapshot.link_target == record.link_target:
            snapshots[relative_target] = snapshot
    return snapshots


def _created_reconcile_link_snapshots(
    home: Path,
    transaction: ReconcileTransaction | None,
) -> dict[PurePosixPath, SymlinkSnapshot]:
    snapshots: dict[PurePosixPath, SymlinkSnapshot] = {}
    if transaction is None:
        return snapshots
    for mutation in transaction.mutations:
        if mutation.created_snapshot is None:
            continue
        try:
            relative_target = mutation.action.target.relative_to(home)
        except ValueError as error:
            raise SyncError(
                f"managed target is outside home: {mutation.action.target}"
            ) from error
        key = PurePosixPath(*relative_target.parts)
        if key in snapshots:
            raise SyncError(f"duplicate created managed-link snapshot: {key}")
        snapshots[key] = mutation.created_snapshot
    return snapshots


def _trusted_managed_link_snapshots_for_state(
    home: Path,
    state: ManagedState,
    baseline_snapshots: dict[PurePosixPath, SymlinkSnapshot],
    transaction: ReconcileTransaction | None,
) -> dict[PurePosixPath, SymlinkSnapshot]:
    created_snapshots = _created_reconcile_link_snapshots(home, transaction)
    snapshots: dict[PurePosixPath, SymlinkSnapshot] = {}
    for relative_target, record in sorted(
        state.links.items(),
        key=lambda item: item[0].as_posix(),
    ):
        snapshot = created_snapshots.get(relative_target)
        if snapshot is None:
            snapshot = baseline_snapshots.get(relative_target)
        if snapshot is None:
            raise SyncError(
                f"managed link has no trusted identity snapshot: {relative_target}"
            )
        if snapshot.link_target != record.link_target:
            raise SyncError(
                f"managed link target changed before publication: {relative_target}"
            )
        target = home / Path(*relative_target.parts)
        try:
            current_snapshot = _read_symlink_snapshot_beneath(home, target)
        except (FileNotFoundError, OSError, SyncError) as error:
            raise SyncError(
                f"managed link changed before publication: {relative_target}"
            ) from error
        if current_snapshot != snapshot:
            raise SyncError(
                f"managed link changed before publication: {relative_target}"
            )
        snapshots[relative_target] = snapshot
    return snapshots


def _verify_managed_link_snapshots(
    home: Path,
    state: ManagedState,
    snapshots: dict[PurePosixPath, SymlinkSnapshot],
) -> None:
    if set(snapshots) != set(state.links):
        raise SyncError("managed link snapshot targets changed after publication")
    for relative_target, record in sorted(
        state.links.items(),
        key=lambda item: item[0].as_posix(),
    ):
        expected_snapshot = snapshots[relative_target]
        if expected_snapshot.link_target != record.link_target:
            raise SyncError(
                f"managed link target changed after publication: {relative_target}"
            )
        target = home / Path(*relative_target.parts)
        try:
            current_snapshot = _read_symlink_snapshot_beneath(home, target)
        except (FileNotFoundError, OSError, SyncError) as error:
            raise SyncError(
                f"managed link identity changed after release validation: "
                f"{relative_target}"
            ) from error
        if current_snapshot != expected_snapshot:
            raise SyncError(
                f"managed link identity changed after release validation: "
                f"{relative_target}"
            )


def _verify_published_transaction_matches_desired(
    home: Path,
    desired_entries: list[LinkEntry],
    expected_state: ManagedState,
    transaction: ManagedStateFileTransaction | None,
    managed_link_snapshots: dict[PurePosixPath, SymlinkSnapshot],
) -> None:
    _verify_published_state_transaction(home, transaction)
    _verify_published_state_matches_desired(
        home,
        desired_entries,
        expected_state,
    )
    _verify_desired_entries(home, desired_entries)
    _verify_managed_link_snapshots(
        home,
        expected_state,
        managed_link_snapshots,
    )
    _verify_published_state_transaction(home, transaction)


def _managed_targets_after_reconciliation(
    home: Path,
    state: ManagedState,
    actions: list[ReconcileAction],
) -> set[PurePosixPath]:
    managed_targets = set(state.links)
    for action in actions:
        if action.action not in {"create", "replace", "quarantine-replace"}:
            continue
        try:
            relative_target = action.target.relative_to(home)
        except ValueError as error:
            raise SyncError(
                f"managed target is outside home: {action.target}"
            ) from error
        if not relative_target.parts:
            raise SyncError(f"managed target must not be sync home: {action.target}")
        managed_targets.add(PurePosixPath(*relative_target.parts))
    return managed_targets


def plan_link_actions(
    home: Path,
    entries: list[LinkEntry],
    *,
    public_entries: list[LinkEntry] | None = None,
    pending_public_removals: set[Path] | None = None,
) -> list[LinkAction]:
    actions: list[LinkAction] = []
    pending_public_removals = pending_public_removals or set()
    entry_owners = {entry.owner for entry in entries}
    public_by_target = (
        _entries_by_target(
            public_entries
            if public_entries is not None
            else current_release_entries(home, PUBLIC_OWNER)
        )
        if any(owner != PUBLIC_OWNER for owner in entry_owners)
        else {}
    )
    for entry in entries:
        target = _entry_target_path(home, entry)
        desired = _desired_link_target(home, entry)
        parent = target.parent
        public_entry = public_by_target.get(entry.target)
        if entry.owner != PUBLIC_OWNER:
            if (
                public_entry is not None
                and not entry.override
                and entry.target not in OPTIONAL_PUBLIC_TARGETS
            ):
                raise SyncError(
                    f"target {target} exists in public manifest; "
                    f"manifest owner {entry.owner} must declare override=true"
                )
            if (
                public_entry is None
                and entry.override
                and entry.target not in OPTIONAL_PUBLIC_TARGETS
            ):
                raise SyncError(f"override target has no public base target: {target}")
        if _path_exists_or_is_link(parent) and not parent.is_dir():
            raise SyncError(f"link parent exists but is not a directory: {parent}")
        if _path_exists_or_is_link(target):
            if not target.is_symlink():
                if entry.owner == PUBLIC_OWNER and entry.target in OPTIONAL_PUBLIC_TARGETS:
                    continue
                raise SyncError(f"refusing to replace non-symlink target: {target}")
            existing = os.readlink(target)
            if existing == desired:
                continue
            existing_owner = _link_managed_owner(home, target, entry_owners)
            if existing_owner is None:
                if entry.owner == PUBLIC_OWNER and entry.target in OPTIONAL_PUBLIC_TARGETS:
                    continue
                raise SyncError(f"refusing to replace unmanaged symlink target: {target}")
            if existing_owner != entry.owner:
                if entry.owner == PUBLIC_OWNER:
                    continue
                if (
                    existing_owner == PUBLIC_OWNER
                    and public_entry is None
                    and target in pending_public_removals
                ):
                    actions.append(LinkAction("replace", target, desired, entry.kind))
                    continue
                if (
                    existing_owner != PUBLIC_OWNER
                    or (not entry.override and entry.target not in OPTIONAL_PUBLIC_TARGETS)
                ):
                    raise SyncError(
                        f"target {target} is managed by {existing_owner}; "
                        f"manifest owner {entry.owner} must declare override=true"
                    )
            actions.append(LinkAction("replace", target, desired, entry.kind))
        else:
            actions.append(LinkAction("create", target, desired, entry.kind))
    return actions


def apply_link_actions(actions: list[LinkAction], *, dry_run: bool) -> None:
    for action in actions:
        if dry_run:
            if action.action == "remove":
                print(f"would remove stale symlink {action.target}")
            else:
                print(f"would {action.action} symlink {action.target} -> {action.link_target}")
            continue
        if action.action == "remove":
            if action.target.is_symlink():
                action.target.unlink()
                print(f"removed stale symlink {action.target}")
            continue
        action.target.parent.mkdir(parents=True, exist_ok=True)
        if action.target.is_symlink():
            action.target.unlink()
        action.target.symlink_to(
            action.link_target,
            target_is_directory=action.kind in {"directory", "skill"},
        )
        print(f"{action.action}d symlink {action.target} -> {action.link_target}")


def validate_release_tree(release_root: Path) -> list[LinkEntry]:
    return load_manifest(release_root)


def current_release_entries(home: Path, owner: str = PUBLIC_OWNER) -> list[LinkEntry]:
    sha = _current_sha(home, owner)
    if sha is None:
        return []
    return _current_manifest_data(home, owner).entries


def plan_stale_link_removals(
    home: Path,
    previous_entries: list[LinkEntry],
    next_entries: list[LinkEntry],
    *,
    public_entries: list[LinkEntry] | None = None,
) -> list[LinkAction]:
    next_targets = {entry.target for entry in next_entries}
    public_by_target = _entries_by_target(
        public_entries
        if public_entries is not None
        else current_release_entries(home, PUBLIC_OWNER)
    )
    removals: list[LinkAction] = []
    for entry in previous_entries:
        if entry.target in next_targets:
            continue
        target = _entry_target_path(home, entry)
        public_entry = public_by_target.get(entry.target)
        if entry.owner != PUBLIC_OWNER and public_entry is not None:
            if not _path_exists_or_is_link(target) or (
                target.is_symlink() and os.readlink(target) == _desired_link_target(home, entry)
            ):
                removals.append(
                    LinkAction(
                        "restore",
                        target,
                        _desired_link_target(home, public_entry),
                        public_entry.kind,
                    )
                )
            continue
        if target.is_symlink() and os.readlink(target) == _desired_link_target(home, entry):
            removals.append(LinkAction("remove", target, "", entry.kind))
    return removals


def _known_manifest_target_parents(
    home: Path,
    entries: list[LinkEntry],
    owner: str | None = None,
) -> set[Path]:
    parents = {home, home / "agents", home / "bin", home / "skills"}
    parents.update(_entry_target_path(home, entry).parent for entry in entries)
    manifest_owner = _validate_owner(owner) if owner is not None else _entries_owner(entries)
    releases_root = _releases_root(home, manifest_owner)
    if not _ensure_safe_internal_directory(
        home,
        releases_root,
        create=False,
        allow_missing=True,
    ):
        return parents
    for release_dir in releases_root.iterdir():
        if RELEASE_DIR_RE.fullmatch(release_dir.name) is None:
            continue
        mode = os.lstat(release_dir).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SyncError(f"refusing unsafe release directory: {release_dir}")
        try:
            release_entries = _load_installed_manifest_data(
                home,
                manifest_owner,
                release_dir.name,
            ).entries
        except SyncError:
            continue
        parents.update(_entry_target_path(home, entry).parent for entry in release_entries)
    return parents


def _known_manifest_link_targets(
    home: Path,
    entries: list[LinkEntry],
) -> dict[Path, set[str]]:
    targets: dict[Path, set[str]] = {}

    def add_entry(entry: LinkEntry) -> None:
        targets.setdefault(_entry_target_path(home, entry), set()).add(
            _desired_link_target(home, entry)
        )

    for entry in entries:
        add_entry(entry)

    owner = _entries_owner(entries)
    releases_root = _releases_root(home, owner)
    if not _ensure_safe_internal_directory(
        home,
        releases_root,
        create=False,
        allow_missing=True,
    ):
        return targets
    for release_dir in releases_root.iterdir():
        if RELEASE_DIR_RE.fullmatch(release_dir.name) is None:
            continue
        mode = os.lstat(release_dir).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SyncError(f"refusing unsafe release directory: {release_dir}")
        try:
            release_entries = _load_installed_manifest_data(
                home,
                owner,
                release_dir.name,
            ).entries
        except SyncError:
            continue
        for entry in release_entries:
            add_entry(entry)
    return targets


def find_stale_current_symlinks(home: Path, entries: list[LinkEntry]) -> list[Path]:
    managed_targets = {_entry_target_path(home, entry) for entry in entries}
    candidates: list[Path] = []
    for parent in sorted(_known_manifest_target_parents(home, entries)):
        if parent.is_dir():
            candidates.extend(parent.iterdir())

    stale: list[Path] = []
    owner = _entries_owner(entries)
    current_root = _current_link(home, owner).resolve(strict=False)
    for candidate in candidates:
        if candidate in managed_targets or not candidate.is_symlink():
            continue
        linked_path = (candidate.parent / os.readlink(candidate)).resolve(strict=False)
        try:
            linked_path.relative_to(current_root)
        except ValueError:
            continue
        else:
            stale.append(candidate)
    return stale


def plan_stale_current_link_removals(
    home: Path,
    entries: list[LinkEntry],
) -> list[LinkAction]:
    known_targets = _known_manifest_link_targets(home, entries)
    removals: list[LinkAction] = []
    for stale_link in find_stale_current_symlinks(home, entries):
        expected_targets = known_targets.get(stale_link)
        if expected_targets is None or os.readlink(stale_link) not in expected_targets:
            continue
        removals.append(LinkAction("remove", stale_link, "", "directory"))
    return removals


def _dedupe_link_actions(actions: list[LinkAction]) -> list[LinkAction]:
    deduped: list[LinkAction] = []
    seen: set[tuple[str, Path]] = set()
    for action in actions:
        key = (action.action, action.target)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _copy_bytes(
    source_fd: int,
    destination_fd: int,
    expected_size: int,
    display_path: Path,
) -> None:
    remaining = expected_size
    while remaining:
        chunk = os.read(source_fd, min(1024 * 1024, remaining))
        if not chunk:
            raise SyncError(
                f"release source ended before captured size during copy: {display_path}"
            )
        remaining -= len(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:
                raise SyncError("release copy made no write progress")
            view = view[written:]
    if os.read(source_fd, 1):
        raise SyncError(f"release source grew during copy: {display_path}")


@dataclass(frozen=True)
class _ReleaseSourceSnapshot:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _release_source_snapshot(metadata: os.stat_result) -> _ReleaseSourceSnapshot:
    return _ReleaseSourceSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _release_source_matches(
    snapshot: _ReleaseSourceSnapshot,
    metadata: os.stat_result,
) -> bool:
    return snapshot == _release_source_snapshot(metadata)


def _require_release_source_unchanged(
    snapshot: _ReleaseSourceSnapshot,
    metadata: os.stat_result,
    display_path: Path,
) -> None:
    if not _release_source_matches(snapshot, metadata):
        raise SyncError(f"release source changed during copy: {display_path}")


def _source_directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _source_regular_file_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _open_source_regular_file(
    parent_fd: int,
    name: str,
    named_snapshot: _ReleaseSourceSnapshot,
    display_path: Path,
) -> tuple[int, _ReleaseSourceSnapshot]:
    file_fd = os.open(name, _source_regular_file_flags(), dir_fd=parent_fd)
    try:
        opened_metadata = os.fstat(file_fd)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise SyncError(f"release source is not a regular file: {display_path}")
        opened_snapshot = _release_source_snapshot(opened_metadata)
        if opened_snapshot != named_snapshot:
            raise SyncError(f"release source changed before open: {display_path}")
        return file_fd, opened_snapshot
    except BaseException:
        _close_fd_quietly(file_fd)
        raise


def _read_exact_regular_file(
    file_fd: int,
    snapshot: _ReleaseSourceSnapshot,
    display_path: Path,
    *,
    max_bytes: int | None = None,
) -> bytes:
    if max_bytes is not None and snapshot.size > max_bytes:
        raise SyncError(
            f"release manifest exceeds {max_bytes} bytes: {display_path}"
        )
    chunks: list[bytes] = []
    remaining = snapshot.size
    while remaining:
        chunk = os.read(file_fd, min(1024 * 1024, remaining))
        if not chunk:
            raise SyncError(
                f"release source ended before captured size while reading: {display_path}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(file_fd, 1):
        raise SyncError(f"release source grew while reading: {display_path}")
    return b"".join(chunks)


def _hash_exact_regular_file(
    file_fd: int,
    snapshot: _ReleaseSourceSnapshot,
    display_path: Path,
    *,
    capture_payload: bool,
) -> tuple[bytes, bytes | None]:
    if capture_payload and snapshot.size > MAX_RELEASE_MANIFEST_BYTES:
        raise SyncError(
            f"release manifest exceeds {MAX_RELEASE_MANIFEST_BYTES} bytes: "
            f"{display_path}"
        )
    content_digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if capture_payload else None
    remaining = snapshot.size
    while remaining:
        chunk = os.read(file_fd, min(1024 * 1024, remaining))
        if not chunk:
            raise SyncError(
                f"release source ended before captured size while hashing: {display_path}"
            )
        content_digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(file_fd, 1):
        raise SyncError(f"release source grew while hashing: {display_path}")
    content_identity = snapshot.size.to_bytes(8, "big") + content_digest.digest()
    payload = b"".join(chunks) if chunks is not None else None
    return content_identity, payload


def _open_release_source_root(
    source_root: Path,
) -> tuple[int, int, _ReleaseSourceSnapshot]:
    parent_fd = os.open(source_root.parent, _source_directory_flags())
    source_fd = -1
    try:
        named_metadata = os.stat(
            source_root.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(named_metadata.st_mode):
            raise SyncError(f"release source root is unsafe: {source_root}")
        source_fd = os.open(
            source_root.name,
            _source_directory_flags(),
            dir_fd=parent_fd,
        )
        source_metadata = os.fstat(source_fd)
        snapshot = _release_source_snapshot(source_metadata)
        _require_release_source_unchanged(snapshot, named_metadata, source_root)
        return parent_fd, source_fd, snapshot
    except BaseException as error:
        if source_fd >= 0:
            _close_fd_quietly(source_fd)
        _close_fd_quietly(parent_fd)
        if isinstance(error, OSError):
            raise SyncError(f"release source root is unsafe: {source_root}") from error
        raise


def _open_relative_parent_fd(
    root_fd: int,
    relative_path: PurePosixPath,
) -> tuple[int, str]:
    if not relative_path.parts:
        raise SyncError("release relative path must not be empty")
    parent_fd = os.dup(root_fd)
    try:
        for part in relative_path.parts[:-1]:
            next_fd = os.open(part, _source_directory_flags(), dir_fd=parent_fd)
            _close_fd_quietly(parent_fd)
            parent_fd = next_fd
        return parent_fd, relative_path.parts[-1]
    except BaseException:
        _close_fd_quietly(parent_fd)
        raise


def _release_path_kind_at_fd(
    root_fd: int,
    relative_path: PurePosixPath,
) -> str | None:
    parent_fd = -1
    try:
        parent_fd, name = _open_relative_parent_fd(root_fd, relative_path)
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError, OSError, SyncError):
        return None
    finally:
        if parent_fd >= 0:
            _close_fd_quietly(parent_fd)
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return None


def _read_regular_file_at(
    root_fd: int,
    relative_path: PurePosixPath,
    display_root: Path,
    *,
    max_bytes: int | None = None,
) -> bytes:
    parent_fd = -1
    file_fd = -1
    display_path = display_root / Path(*relative_path.parts)
    try:
        parent_fd, name = _open_relative_parent_fd(root_fd, relative_path)
        named_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(named_metadata.st_mode):
            raise SyncError(f"release source is not a regular file: {display_path}")
        named_snapshot = _release_source_snapshot(named_metadata)
        file_fd, snapshot = _open_source_regular_file(
            parent_fd,
            name,
            named_snapshot,
            display_path,
        )
        payload = _read_exact_regular_file(
            file_fd,
            snapshot,
            display_path,
            max_bytes=max_bytes,
        )
        _require_release_source_unchanged(snapshot, os.fstat(file_fd), display_path)
        current_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_release_source_unchanged(snapshot, current_metadata, display_path)
        return payload
    except OSError as error:
        raise SyncError(f"failed to read release source: {display_path}") from error
    finally:
        if file_fd >= 0:
            _close_fd_quietly(file_fd)
        if parent_fd >= 0:
            _close_fd_quietly(parent_fd)


def _load_manifest_payload_from_directory_fd(
    root_fd: int,
    display_root: Path,
) -> dict[str, Any]:
    relative_manifest = PurePosixPath(MANIFEST_RELATIVE_PATH.as_posix())
    payload = _read_regular_file_at(
        root_fd,
        relative_manifest,
        display_root,
        max_bytes=MAX_RELEASE_MANIFEST_BYTES,
    )
    manifest_path = display_root / MANIFEST_RELATIVE_PATH
    return _decode_manifest_payload(payload, manifest_path)


def _decode_manifest_payload(
    payload: bytes,
    manifest_path: Path,
) -> dict[str, Any]:
    try:
        data = json.loads(
            payload.decode("utf-8"),
            parse_int=_bounded_json_integer,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SyncError(f"Invalid JSON in {manifest_path}: {error}") from error
    if not isinstance(data, dict):
        raise SyncError(f"Expected JSON object in {manifest_path}")
    return data


def _load_manifest_data_from_directory_fd(
    root_fd: int,
    display_root: Path,
) -> tuple[dict[str, Any], ManifestData]:
    data = _load_manifest_payload_from_directory_fd(root_fd, display_root)
    manifest = _parse_manifest_data(
        data,
        lambda relative_path: _release_path_kind_at_fd(root_fd, relative_path),
    )
    return data, manifest


def _open_installed_release_directory_fd(
    home: Path,
    owner: str,
    sha: str,
) -> int:
    owner = _validate_owner(owner)
    sha = _validate_release_sha(sha, f"installed release SHA for owner {owner}")
    release_root = _releases_root(home, owner) / sha
    try:
        release_fd = _open_directory_beneath(home, release_root)
    except (OSError, SyncError) as error:
        raise SyncError(f"refusing unsafe release directory: {release_root}") from error
    if not _bound_directory_matches(home, release_root, release_fd):
        _close_fd_quietly(release_fd)
        raise SyncError(f"installed release directory changed: {release_root}")
    return release_fd


def _release_tree_identity_from_directory_fd(
    root_fd: int,
    display_root: Path,
    *,
    require_sanitized_modes: bool = False,
) -> tuple[dict[str, Any], ManifestData, str]:
    (
        manifest_payload,
        tree_digest,
        path_kinds,
        source_snapshots,
        source_members,
    ) = _release_tree_snapshot_from_directory_fd(
        root_fd,
        display_root,
        require_sanitized_modes=require_sanitized_modes,
    )
    data = _decode_manifest_payload(
        manifest_payload,
        display_root / MANIFEST_RELATIVE_PATH,
    )
    manifest = _parse_manifest_data(data, path_kinds.get)
    _verify_release_source_snapshot(
        root_fd,
        display_root,
        source_snapshots,
        source_members,
        operation="identity validation",
    )
    return data, manifest, tree_digest


def _installed_release_identity_and_directory_identity(
    home: Path,
    owner: str,
    sha: str,
) -> ReleaseTreeExpectation:
    owner = _validate_owner(owner)
    sha = _validate_release_sha(sha, f"installed release SHA for owner {owner}")
    release_root = _releases_root(home, owner) / sha
    release_fd = _open_installed_release_directory_fd(home, owner, sha)
    try:
        directory_identity = _directory_identity(release_fd)
        identity = _release_tree_identity_from_directory_fd(
            release_fd,
            release_root,
            require_sanitized_modes=True,
        )
        if identity[1].owner != owner:
            raise SyncError(
                f"release owner mismatch: expected {owner}, got {identity[1].owner}"
            )
        if not _bound_directory_matches(home, release_root, release_fd):
            raise SyncError(f"installed release directory changed: {release_root}")
        if _directory_identity(release_fd) != directory_identity:
            raise SyncError(f"installed release directory changed: {release_root}")
        return identity, directory_identity
    finally:
        _close_fd_quietly(release_fd)


def _pending_release_expectations_for_state(
    home: Path,
    state: ManagedState,
    cache: dict[tuple[str, str], PendingReleaseExpectation] | None = None,
) -> tuple[PendingReleaseExpectation, ...]:
    if len(state.owners) > MAX_PENDING_RELEASES:
        raise SyncError("pending transaction has too many release expectations")
    if cache is None:
        cache = {}
    expectations: list[PendingReleaseExpectation] = []
    for owner, sha in sorted(state.owners.items()):
        key = (owner, sha)
        expectation = cache.get(key)
        if expectation is None:
            release_identity, directory_identity = (
                _installed_release_identity_and_directory_identity(
                    home,
                    owner,
                    sha,
                )
            )
            tree_sha256 = release_identity[2]
            if re.fullmatch(r"[0-9a-f]{64}", tree_sha256) is None:
                raise SyncError(
                    f"installed release tree digest is invalid: {owner}@{sha}"
                )
            expectation = PendingReleaseExpectation(
                owner=owner,
                sha=sha,
                directory_identity=directory_identity,
                tree_sha256=tree_sha256,
            )
            cache[key] = expectation
        expectations.append(expectation)
    return tuple(expectations)


def _installed_release_identity(
    home: Path,
    owner: str,
    sha: str,
) -> ReleaseTreeIdentity:
    identity, _directory_identity_value = (
        _installed_release_identity_and_directory_identity(home, owner, sha)
    )
    return identity


def _load_installed_manifest_data(
    home: Path,
    owner: str,
    sha: str,
) -> ManifestData:
    _payload, manifest, _tree_digest = _installed_release_identity(home, owner, sha)
    return manifest


def _sanitized_release_mode(metadata: os.stat_result) -> int:
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISDIR(metadata.st_mode):
        return (mode & 0o755) | 0o700
    return mode & 0o755


def _release_tree_digest_field(digest: Any, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _release_tree_snapshot_from_directory_fd(
    root_fd: int,
    display_root: Path,
    *,
    require_sanitized_modes: bool,
) -> tuple[
    bytes,
    str,
    dict[PurePosixPath, str],
    dict[PurePosixPath, _ReleaseSourceSnapshot],
    dict[PurePosixPath, tuple[str, ...]],
]:
    """Capture one normalized release-tree identity without following symlinks."""
    digest = hashlib.sha256(b"codex-personal-sync-release-tree-v1\0")
    manifest_relative = PurePosixPath(MANIFEST_RELATIVE_PATH.as_posix())
    manifest_payload: bytes | None = None
    path_kinds: dict[PurePosixPath, str] = {}
    source_snapshots: dict[PurePosixPath, _ReleaseSourceSnapshot] = {}
    source_members: dict[PurePosixPath, tuple[str, ...]] = {}

    def normalized_mode(metadata: os.stat_result, display_path: Path) -> int:
        actual_mode = stat.S_IMODE(metadata.st_mode)
        expected_mode = _sanitized_release_mode(metadata)
        if require_sanitized_modes and actual_mode != expected_mode:
            raise SyncError(
                "release tree entry mode is not sanitized: "
                f"{display_path}: {actual_mode:#o} != {expected_mode:#o}"
            )
        return expected_mode

    def record(
        entry_type: bytes,
        relative_path: PurePosixPath,
        mode: int,
        content_digest: bytes = b"",
    ) -> None:
        relative_bytes = (
            relative_path.as_posix().encode("utf-8", "surrogateescape")
            if relative_path.parts
            else b""
        )
        _release_tree_digest_field(digest, entry_type)
        _release_tree_digest_field(digest, relative_bytes)
        _release_tree_digest_field(digest, mode.to_bytes(4, "big"))
        _release_tree_digest_field(digest, content_digest)

    def visit_directory(
        directory_fd: int,
        relative_root: PurePosixPath,
    ) -> None:
        nonlocal manifest_payload
        display_directory = display_root / Path(*relative_root.parts)
        directory_metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise SyncError(f"release tree entry is not a directory: {display_directory}")
        directory_snapshot = _release_source_snapshot(directory_metadata)
        source_snapshots[relative_root] = directory_snapshot
        path_kinds[relative_root] = "directory"
        directory_mode = normalized_mode(directory_metadata, display_directory)
        record(
            b"directory",
            relative_root,
            # The release root is an installation container. Its safe 0700/0755
            # mode can vary by extractor without changing packaged content.
            0 if not relative_root.parts else directory_mode,
        )
        names = _directory_member_names(directory_fd)
        source_members[relative_root] = names
        for name in names:
            relative_path = relative_root / name
            display_path = display_root / Path(*relative_path.parts)
            try:
                named_metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise SyncError(f"release tree changed while hashing: {display_path}") from error
            snapshot = _release_source_snapshot(named_metadata)
            source_snapshots[relative_path] = snapshot
            if stat.S_ISDIR(named_metadata.st_mode):
                path_kinds[relative_path] = "directory"
                child_fd = -1
                try:
                    child_fd = os.open(
                        name,
                        _source_directory_flags(),
                        dir_fd=directory_fd,
                    )
                    _require_release_source_unchanged(
                        snapshot,
                        os.fstat(child_fd),
                        display_path,
                    )
                    visit_directory(child_fd, relative_path)
                    _require_release_source_unchanged(
                        snapshot,
                        os.fstat(child_fd),
                        display_path,
                    )
                    current_metadata = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    _require_release_source_unchanged(
                        snapshot,
                        current_metadata,
                        display_path,
                    )
                except OSError as error:
                    raise SyncError(
                        f"release tree changed while hashing: {display_path}"
                    ) from error
                finally:
                    if child_fd >= 0:
                        _close_fd_quietly(child_fd)
                continue
            if not stat.S_ISREG(named_metadata.st_mode):
                raise SyncError(f"refusing unsafe release tree entry: {display_path}")
            path_kinds[relative_path] = "file"
            file_fd = -1
            try:
                file_fd, opened_snapshot = _open_source_regular_file(
                    directory_fd,
                    name,
                    snapshot,
                    display_path,
                )
                file_identity, captured_payload = _hash_exact_regular_file(
                    file_fd,
                    opened_snapshot,
                    display_path,
                    capture_payload=relative_path == manifest_relative,
                )
                if captured_payload is not None:
                    manifest_payload = captured_payload
                _require_release_source_unchanged(
                    opened_snapshot,
                    os.fstat(file_fd),
                    display_path,
                )
                current_metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                _require_release_source_unchanged(
                    opened_snapshot,
                    current_metadata,
                    display_path,
                )
                record(
                    b"file",
                    relative_path,
                    normalized_mode(named_metadata, display_path),
                    file_identity,
                )
            except OSError as error:
                raise SyncError(
                    f"release tree changed while hashing: {display_path}"
                ) from error
            finally:
                if file_fd >= 0:
                    _close_fd_quietly(file_fd)
        if _directory_member_names(directory_fd) != names:
            raise SyncError(
                f"release tree directory changed while hashing: {display_directory}"
            )
        _require_release_source_unchanged(
            directory_snapshot,
            os.fstat(directory_fd),
            display_directory,
        )

    visit_directory(root_fd, PurePosixPath())
    if manifest_payload is None:
        raise SyncError(f"release manifest is missing: {display_root / MANIFEST_RELATIVE_PATH}")
    return (
        manifest_payload,
        digest.hexdigest(),
        path_kinds,
        source_snapshots,
        source_members,
    )


def _release_tree_digest_from_directory_fd(
    root_fd: int,
    display_root: Path,
    *,
    require_sanitized_modes: bool = False,
) -> str:
    _payload, _manifest, digest = _release_tree_identity_from_directory_fd(
        root_fd,
        display_root,
        require_sanitized_modes=require_sanitized_modes,
    )
    return digest


def _directory_member_names(directory_fd: int) -> tuple[str, ...]:
    return tuple(sorted(entry.name for entry in os.scandir(directory_fd)))


def _open_source_directory_entry(
    parent_fd: int,
    name: str,
    snapshot: _ReleaseSourceSnapshot,
    display_path: Path,
) -> int:
    child_fd = os.open(name, _source_directory_flags(), dir_fd=parent_fd)
    try:
        _require_release_source_unchanged(snapshot, os.fstat(child_fd), display_path)
        return child_fd
    except BaseException:
        _close_fd_quietly(child_fd)
        raise


def _copy_tree_from_directory_fd(
    source_fd: int,
    destination_fd: int,
    display_root: Path,
    relative_root: PurePosixPath,
    source_snapshots: dict[PurePosixPath, _ReleaseSourceSnapshot],
    source_members: dict[PurePosixPath, tuple[str, ...]],
) -> None:
    directory_metadata = os.fstat(source_fd)
    if not stat.S_ISDIR(directory_metadata.st_mode):
        raise SyncError(f"release source is not a directory: {display_root}")
    directory_snapshot = _release_source_snapshot(directory_metadata)
    source_snapshots[relative_root] = directory_snapshot
    names = _directory_member_names(source_fd)
    source_members[relative_root] = names
    for name in names:
        relative_path = relative_root / name
        display_path = display_root / Path(*relative_path.parts)
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        snapshot = _release_source_snapshot(metadata)
        source_snapshots[relative_path] = snapshot
        if stat.S_ISDIR(metadata.st_mode):
            child_source_fd = _open_source_directory_entry(
                source_fd,
                name,
                snapshot,
                display_path,
            )
            child_destination_fd = -1
            try:
                os.mkdir(name, mode=0o700, dir_fd=destination_fd)
                child_destination_fd = os.open(
                    name,
                    _source_directory_flags(),
                    dir_fd=destination_fd,
                )
                _copy_tree_from_directory_fd(
                    child_source_fd,
                    child_destination_fd,
                    display_root,
                    relative_path,
                    source_snapshots,
                    source_members,
                )
                os.fchmod(child_destination_fd, _sanitized_release_mode(metadata))
                os.fsync(child_destination_fd)
                _require_release_source_unchanged(
                    snapshot,
                    os.fstat(child_source_fd),
                    display_path,
                )
                current_metadata = os.stat(
                    name,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                _require_release_source_unchanged(
                    snapshot,
                    current_metadata,
                    display_path,
                )
            finally:
                _close_fd_quietly(child_source_fd)
                if child_destination_fd >= 0:
                    _close_fd_quietly(child_destination_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SyncError(f"refusing unsafe release source entry: {display_path}")
        child_source_fd, opened_snapshot = _open_source_regular_file(
            source_fd,
            name,
            snapshot,
            display_path,
        )
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        destination_flags |= getattr(os, "O_CLOEXEC", 0)
        destination_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_fd = -1
        try:
            file_fd = os.open(
                name,
                destination_flags,
                0o600,
                dir_fd=destination_fd,
            )
            _copy_bytes(
                child_source_fd,
                file_fd,
                opened_snapshot.size,
                display_path,
            )
            _require_release_source_unchanged(
                opened_snapshot,
                os.fstat(child_source_fd),
                display_path,
            )
            current_metadata = os.stat(
                name,
                dir_fd=source_fd,
                follow_symlinks=False,
            )
            _require_release_source_unchanged(
                opened_snapshot,
                current_metadata,
                display_path,
            )
            os.fchmod(file_fd, _sanitized_release_mode(metadata))
            os.fsync(file_fd)
        finally:
            _close_fd_quietly(child_source_fd)
            if file_fd >= 0:
                _close_fd_quietly(file_fd)
    if _directory_member_names(source_fd) != names:
        raise SyncError(f"release source directory changed during copy: {display_root}")
    _require_release_source_unchanged(
        directory_snapshot,
        os.fstat(source_fd),
        display_root / Path(*relative_root.parts),
    )


def _source_metadata_at_relative(
    root_fd: int,
    relative_path: PurePosixPath,
) -> os.stat_result:
    if not relative_path.parts:
        return os.fstat(root_fd)
    parent_fd, name = _open_relative_parent_fd(root_fd, relative_path)
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        _close_fd_quietly(parent_fd)


def _source_directory_members_at_relative(
    root_fd: int,
    relative_path: PurePosixPath,
) -> tuple[str, ...]:
    if not relative_path.parts:
        return _directory_member_names(root_fd)
    parent_fd, name = _open_relative_parent_fd(root_fd, relative_path)
    directory_fd = -1
    try:
        directory_fd = os.open(name, _source_directory_flags(), dir_fd=parent_fd)
        return _directory_member_names(directory_fd)
    finally:
        if directory_fd >= 0:
            _close_fd_quietly(directory_fd)
        _close_fd_quietly(parent_fd)


def _verify_release_source_snapshot(
    root_fd: int,
    display_root: Path,
    source_snapshots: dict[PurePosixPath, _ReleaseSourceSnapshot],
    source_members: dict[PurePosixPath, tuple[str, ...]],
    *,
    operation: str = "copy",
) -> None:
    for relative_path, snapshot in source_snapshots.items():
        display_path = display_root / Path(*relative_path.parts)
        try:
            metadata = _source_metadata_at_relative(root_fd, relative_path)
        except OSError as error:
            raise SyncError(
                f"release source changed during {operation}: {display_path}"
            ) from error
        if not _release_source_matches(snapshot, metadata):
            raise SyncError(
                f"release source changed during {operation}: {display_path}"
            )
    for relative_path, expected_names in source_members.items():
        display_path = display_root / Path(*relative_path.parts)
        try:
            current_names = _source_directory_members_at_relative(root_fd, relative_path)
        except OSError as error:
            raise SyncError(
                f"release source directory changed during {operation}: {display_path}"
            ) from error
        if current_names != expected_names:
            raise SyncError(
                f"release source directory changed during {operation}: {display_path}"
            )


def _named_entry_identity(parent_fd: int, name: str) -> tuple[int, int] | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino


def _retain_named_entries(
    parent_fd: int,
    canonical_name: str,
    release_name: str,
) -> tuple[str, ...]:
    retained_entries: list[tuple[str, tuple[int, int]]] = []

    def verified_names() -> tuple[str, ...]:
        for retained_name, retained_identity in retained_entries:
            if _named_entry_identity(parent_fd, retained_name) != retained_identity:
                raise SyncError(
                    "retained release object changed during quarantine: "
                    f"{retained_name}"
                )
        return tuple(name for name, _identity in retained_entries)

    for attempt in range(100):
        identity = _named_entry_identity(parent_fd, canonical_name)
        if identity is None:
            return verified_names()
        retained_name = (
            f".retained-{release_name}-{os.getpid()}-{time.time_ns()}-{attempt}"
        )
        try:
            _rename_noreplace_at(
                parent_fd,
                canonical_name,
                parent_fd,
                retained_name,
            )
        except (FileExistsError, FileNotFoundError):
            continue
        retained_identity = _named_entry_identity(parent_fd, retained_name)
        if retained_identity is None:
            raise SyncError(
                f"retained release object disappeared during quarantine: {retained_name}"
            )
        # If the identity differs from the pre-rename observation, the canonical
        # name raced again. The atomic rename still preserved the object it moved;
        # record and revalidate that actual retained identity before returning.
        if (
            retained_identity != identity
            and _named_entry_identity(parent_fd, retained_name) != retained_identity
        ):
            raise SyncError(
                f"retained release object changed during quarantine: {retained_name}"
            )
        retained_entries.append((retained_name, retained_identity))
        if _named_entry_identity(parent_fd, canonical_name) is None:
            return verified_names()
    raise SyncError(
        f"could not conservatively clear changing release name: {canonical_name}"
    )


def _source_release_identity(
    source_root: Path,
    expected_manifest: ManifestData | None,
    expected_source: ReleaseTreeExpectation | None = None,
) -> ReleaseTreeExpectation:
    parent_fd, source_fd, source_root_snapshot = _open_release_source_root(source_root)
    try:
        identity = _release_tree_identity_from_directory_fd(source_fd, source_root)
        if expected_manifest is not None and identity[1] != expected_manifest:
            raise SyncError("release manifest changed after install preflight")
        source_expectation = (identity, _directory_identity(source_fd))
        if expected_source is not None and source_expectation != expected_source:
            raise SyncError(
                "release source changed after its captured identity"
            )
        _require_release_source_unchanged(
            source_root_snapshot,
            os.fstat(source_fd),
            source_root,
        )
        current_source_root = os.stat(
            source_root.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        _require_release_source_unchanged(
            source_root_snapshot,
            current_source_root,
            source_root,
        )
        return source_expectation
    except OSError as error:
        raise SyncError(f"release source changed during validation: {source_root}") from error
    finally:
        _close_fd_quietly(source_fd)
        _close_fd_quietly(parent_fd)


def _require_existing_release_matches_source(
    source_root: Path,
    home: Path,
    owner: str,
    sha: str,
    expected_manifest: ManifestData,
    expected_source: ReleaseTreeExpectation,
) -> ReleaseTreeExpectation:
    sha = _validate_release_sha(sha)
    release_root = _releases_root(home, owner) / sha
    if Path(os.path.abspath(source_root)) == Path(os.path.abspath(release_root)):
        source_expectation = _installed_release_identity_and_directory_identity(
            home,
            owner,
            sha,
        )
        if source_expectation != expected_source:
            raise SyncError("release source changed after its captured identity")
    else:
        source_expectation = _source_release_identity(
            source_root,
            expected_manifest,
            expected_source,
        )
    installed_identity, installed_directory_identity = (
        _installed_release_identity_and_directory_identity(home, owner, sha)
    )
    if Path(os.path.abspath(source_root)) == Path(os.path.abspath(release_root)):
        current_source_expectation = (
            _installed_release_identity_and_directory_identity(home, owner, sha)
        )
    else:
        current_source_expectation = _source_release_identity(
            source_root,
            expected_manifest,
            expected_source,
        )
    if (
        current_source_expectation != source_expectation
        or current_source_expectation != expected_source
    ):
        raise SyncError("incoming release source changed during identity validation")
    source_identity, _source_directory_identity = current_source_expectation
    current_installed_identity, current_directory_identity = (
        _installed_release_identity_and_directory_identity(home, owner, sha)
    )
    if (
        current_installed_identity != installed_identity
        or current_directory_identity != installed_directory_identity
    ):
        raise SyncError(
            "installed release changed during identity validation; "
            f"preserving installed release {owner}@{sha}"
        )
    installed_identity = current_installed_identity
    source_payload, source_manifest, source_tree_digest = source_identity
    installed_payload, installed_manifest, installed_tree_digest = installed_identity
    if source_manifest != expected_manifest:
        raise SyncError("release manifest changed after install preflight")
    if (
        installed_manifest != expected_manifest
        or installed_payload != source_payload
        or installed_tree_digest != source_tree_digest
    ):
        raise SyncError(
            "existing release tree does not match incoming source; "
            f"preserving installed release {owner}@{sha}"
        )
    return source_identity, current_directory_identity


def _copy_release_tree(
    source_root: Path,
    release_dir: Path,
    home: Path,
    expected_manifest: ManifestData,
    expected_source: ReleaseTreeExpectation,
) -> ReleaseTreeExpectation:
    source_parent_fd, source_fd, source_root_snapshot = _open_release_source_root(
        source_root
    )
    releases_root = release_dir.parent
    releases_fd = _open_or_create_directory_beneath(
        home,
        releases_root,
        mode=0o700,
    )
    temp_name = ""
    temp_fd = -1
    published = False
    try:
        (
            source_payload,
            source_manifest,
            source_tree_digest,
        ) = _release_tree_identity_from_directory_fd(
            source_fd,
            source_root,
        )
        current_source_expectation = (
            (source_payload, source_manifest, source_tree_digest),
            _directory_identity(source_fd),
        )
        if current_source_expectation != expected_source:
            raise SyncError("release source changed after its captured identity")
        if source_manifest != expected_manifest:
            raise SyncError("release manifest changed after install preflight")
        if not _bound_directory_matches(home, releases_root, releases_fd):
            raise SyncError(f"release parent changed before copy: {releases_root}")
        for attempt in range(100):
            temp_name = (
                f".tmp-{release_dir.name}-{os.getpid()}-{time.time_ns()}-{attempt}"
            )
            try:
                os.mkdir(temp_name, mode=0o700, dir_fd=releases_fd)
            except FileExistsError:
                continue
            break
        else:
            raise SyncError(f"could not allocate release staging directory: {release_dir}")
        temp_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        temp_flags |= getattr(os, "O_CLOEXEC", 0)
        temp_flags |= getattr(os, "O_NOFOLLOW", 0)
        temp_fd = os.open(temp_name, temp_flags, dir_fd=releases_fd)
        staged_identity = _directory_identity(temp_fd)
        source_snapshots: dict[PurePosixPath, _ReleaseSourceSnapshot] = {}
        source_members: dict[PurePosixPath, tuple[str, ...]] = {}
        _copy_tree_from_directory_fd(
            source_fd,
            temp_fd,
            source_root,
            PurePosixPath(),
            source_snapshots,
            source_members,
        )
        _verify_release_source_snapshot(
            source_fd,
            source_root,
            source_snapshots,
            source_members,
        )
        _require_release_source_unchanged(
            source_root_snapshot,
            os.fstat(source_fd),
            source_root,
        )
        current_source_root = os.stat(
            source_root.name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
        _require_release_source_unchanged(
            source_root_snapshot,
            current_source_root,
            source_root,
        )
        os.fchmod(temp_fd, _sanitized_release_mode(os.fstat(source_fd)))
        os.fsync(temp_fd)
        (
            staged_payload,
            staged_manifest,
            staged_tree_digest,
        ) = _release_tree_identity_from_directory_fd(
            temp_fd,
            release_dir,
            require_sanitized_modes=True,
        )
        if staged_payload != source_payload or staged_manifest != expected_manifest:
            raise SyncError("staged release manifest differs from install preflight")
        if staged_tree_digest != source_tree_digest:
            raise SyncError("staged release tree differs from install preflight")
        if not _bound_directory_matches(home, releases_root, releases_fd):
            raise SyncError(f"release parent changed during copy: {releases_root}")
        if _named_entry_identity(releases_fd, temp_name) != staged_identity:
            retained = _retain_named_entries(
                releases_fd,
                temp_name,
                release_dir.name,
            )
            temp_name = ""
            raise SyncError(
                "release staging changed before publication; canonical staging "
                f"was retained as {', '.join(retained) or 'an unknown name'}"
            )
        _rename_noreplace_at(
            releases_fd,
            temp_name,
            releases_fd,
            release_dir.name,
        )
        published = True
        temp_name = ""
        if _named_entry_identity(releases_fd, release_dir.name) != staged_identity:
            retained = _retain_named_entries(
                releases_fd,
                release_dir.name,
                release_dir.name,
            )
            os.fsync(releases_fd)
            raise SyncError(
                "published release changed during install; canonical object was "
                f"retained as {', '.join(retained) or 'an unknown name'}"
            )
        published_fd = -1
        try:
            published_fd = os.open(
                release_dir.name,
                _source_directory_flags(),
                dir_fd=releases_fd,
            )
            if _directory_identity(published_fd) != staged_identity:
                raise SyncError("published release identity differs from staging")
            (
                published_payload,
                published_manifest,
                published_tree_digest,
            ) = _release_tree_identity_from_directory_fd(
                published_fd,
                release_dir,
                require_sanitized_modes=True,
            )
            if (
                published_payload != source_payload
                or published_manifest != expected_manifest
            ):
                raise SyncError("published release manifest differs from install preflight")
            if published_tree_digest != source_tree_digest:
                raise SyncError("published release tree differs from install preflight")
        except OSError as error:
            raise SyncError("published release changed during validation") from error
        finally:
            if published_fd >= 0:
                _close_fd_quietly(published_fd)
        os.fsync(releases_fd)
        if not _bound_directory_matches(home, releases_root, releases_fd):
            retained = _retain_named_entries(
                releases_fd,
                release_dir.name,
                release_dir.name,
            )
            os.fsync(releases_fd)
            raise SyncError(
                "release parent changed during publication; staged tree was retained "
                f"as {', '.join(retained) or 'an unknown name'}"
            )
        if _named_entry_identity(releases_fd, release_dir.name) != staged_identity:
            retained = _retain_named_entries(
                releases_fd,
                release_dir.name,
                release_dir.name,
            )
            os.fsync(releases_fd)
            raise SyncError(
                "published release changed after verification; canonical object was "
                f"retained as {', '.join(retained) or 'an unknown name'}"
            )
        return (
            (source_payload, source_manifest, source_tree_digest),
            staged_identity,
        )
    except BaseException:
        if temp_name:
            try:
                _retain_named_entries(releases_fd, temp_name, release_dir.name)
            except (OSError, SyncError) as retain_error:
                raise SyncError(
                    "release installation failed and staging retention was incomplete"
                ) from retain_error
            temp_name = ""
        if published:
            try:
                _retain_named_entries(
                    releases_fd,
                    release_dir.name,
                    release_dir.name,
                )
            except (OSError, SyncError) as retain_error:
                raise SyncError(
                    "release installation failed and publication retention was incomplete"
                ) from retain_error
        raise
    finally:
        if temp_fd >= 0:
            _close_fd_quietly(temp_fd)
        _close_fd_quietly(releases_fd)
        _close_fd_quietly(source_fd)
        _close_fd_quietly(source_parent_fd)


def _ensure_install_roots(home: Path, owner: str) -> None:
    _ensure_safe_internal_directory(
        home,
        _releases_root(home, owner),
        create=True,
    )


def _ensure_current_can_switch(home: Path, owner: str) -> None:
    current = _current_link(home, owner)
    if not _ensure_safe_internal_parent(
        home,
        current,
        create=False,
        allow_missing=True,
    ):
        return
    if _path_exists_or_is_link(current) and not stat.S_ISLNK(os.lstat(current).st_mode):
        raise SyncError(f"refusing to replace non-symlink current pointer: {current}")


def _plan_current_switch_action(
    home: Path,
    sha: str,
    owner: str = PUBLIC_OWNER,
) -> ReconcileAction | None:
    sha = _validate_release_sha(sha)
    current = _current_link(home, owner)
    _ensure_current_can_switch(home, owner)
    next_target = f"releases/{sha}"
    planned_snapshot = _capture_reconcile_target_snapshot(home, current)
    if planned_snapshot.link_identity is None:
        return ReconcileAction(
            "create",
            current,
            next_target,
            "directory",
            planned_snapshot=planned_snapshot,
        )
    previous_target = planned_snapshot.link_target
    if previous_target is None:
        raise SyncError(f"refusing unsafe current pointer: {current}")
    if previous_target == next_target:
        return None
    return ReconcileAction(
        "replace",
        current,
        next_target,
        "directory",
        expected_link_target=previous_target,
        planned_snapshot=planned_snapshot,
    )


def _plan_current_switch_actions(
    home: Path,
    releases: list[tuple[Path, str, ManifestData, ReleaseTreeExpectation]],
) -> list[ReconcileAction]:
    actions: list[ReconcileAction] = []
    seen_owners: set[str] = set()
    for _source_root, sha, manifest, _source_expectation in releases:
        sha = _validate_release_sha(sha)
        if manifest.owner in seen_owners:
            raise SyncError(f"duplicate release owner in install set: {manifest.owner}")
        seen_owners.add(manifest.owner)
        action = _plan_current_switch_action(home, sha, manifest.owner)
        if action is not None:
            actions.append(action)
    return actions


def _plan_current_switch_capacity_actions(
    home: Path,
    releases: list[tuple[Path, str, ManifestData, ReleaseTreeExpectation]],
) -> list[ReconcileAction]:
    actions: list[ReconcileAction] = []
    seen_owners: set[str] = set()
    for _source_root, sha, manifest, _source_expectation in releases:
        sha = _validate_release_sha(sha)
        if manifest.owner in seen_owners:
            raise SyncError(f"duplicate release owner in install set: {manifest.owner}")
        seen_owners.add(manifest.owner)
        actions.append(
            ReconcileAction(
                "replace",
                _current_link(home, manifest.owner),
                f"releases/{sha}",
                "directory",
                expected_link_target=_MAX_PENDING_LINK_TARGET,
            )
        )
    return actions


def _switch_current(
    home: Path,
    sha: str,
    owner: str = PUBLIC_OWNER,
    *,
    dry_run: bool,
) -> None:
    current = _current_link(home, owner)
    if dry_run and not _path_exists_or_is_link(home):
        print(f"would switch {current} -> releases/{sha}")
        return
    action = _plan_current_switch_action(home, sha, owner)
    if action is None:
        print(f"current pointer already uses releases/{sha}: {current}")
        return
    if dry_run:
        print(f"would switch {current} -> releases/{sha}")
        return
    transaction = _apply_reconcile_actions(home, [action], dry_run=False)
    _commit_reconcile_transaction(transaction)
    print(f"switched {current} -> releases/{sha}")



def _installed_manifests(home: Path) -> dict[str, ManifestData]:
    manifests: dict[str, ManifestData] = {}
    for owner in sorted(_known_owners(home)):
        if _current_sha(home, owner) is None:
            continue
        manifests[owner] = _current_manifest_data(home, owner)
    return manifests


def _capture_active_release_expectations(
    home: Path,
    manifests: dict[str, ManifestData],
) -> dict[str, ActiveReleaseExpectation]:
    active: dict[str, ActiveReleaseExpectation] = {}
    for owner, manifest in sorted(manifests.items()):
        sha = _current_sha(home, owner)
        if sha is None:
            raise SyncError(f"current release is missing for owner {owner}")
        expectation = _installed_release_identity_and_directory_identity(
            home,
            owner,
            sha,
        )
        current_sha = _current_sha(home, owner)
        if current_sha != sha:
            raise SyncError(
                f"current release changed during identity capture for owner {owner}"
            )
        if expectation[0][1] != manifest:
            raise SyncError(
                f"current release manifest changed during identity capture for owner {owner}"
            )
        active[owner] = ActiveReleaseExpectation(
            owner=owner,
            sha=sha,
            manifest=manifest,
            expectation=expectation,
        )
    return active


def _verify_install_release_canonical_binding(
    home: Path,
    binding: InstallReleaseBinding,
) -> None:
    if not _bound_directory_matches(
        home,
        binding.releases_root,
        binding.releases_fd,
    ):
        raise SyncError("release parent identity mismatch")
    if (
        _named_entry_identity(binding.releases_fd, binding.sha)
        != binding.expected_directory_identity
    ):
        raise SyncError("canonical release directory identity mismatch")
    if (
        _directory_identity(binding.release_fd)
        != binding.expected_directory_identity
    ):
        raise SyncError("bound release directory identity mismatch")


def _current_release_binding_snapshot(
    home: Path,
    binding: InstallReleaseBinding,
) -> SymlinkSnapshot:
    current = _current_link(home, binding.owner)
    snapshot = _read_symlink_snapshot_beneath(home, current)
    if snapshot.link_target != f"releases/{binding.sha}":
        raise SyncError(f"current release mismatch for owner {binding.owner}")
    return snapshot


def _verify_install_release_binding(
    home: Path,
    binding: InstallReleaseBinding,
    *,
    phase: str,
    verify_current: bool,
) -> None:
    try:
        current_snapshot = (
            _current_release_binding_snapshot(home, binding)
            if verify_current
            else None
        )
        _verify_install_release_canonical_binding(home, binding)
        release_root = binding.releases_root / binding.sha
        current_identity = _release_tree_identity_from_directory_fd(
            binding.release_fd,
            release_root,
            require_sanitized_modes=True,
        )
        if current_identity != binding.expected_identity:
            raise SyncError("complete release identity mismatch")
        if verify_current:
            assert current_snapshot is not None
            if _current_release_binding_snapshot(home, binding) != current_snapshot:
                raise SyncError(
                    f"current release changed during identity validation for owner "
                    f"{binding.owner}"
                )
        _verify_install_release_canonical_binding(home, binding)
    except (OSError, SyncError) as error:
        raise SyncError(
            f"release tree changed {phase}; raced release "
            f"{binding.owner}@{binding.sha} was left in place"
        ) from error


def _verify_install_release_binding_lightweight(
    home: Path,
    binding: InstallReleaseBinding,
    *,
    phase: str,
    verify_current: bool,
) -> None:
    try:
        current_snapshot = (
            _current_release_binding_snapshot(home, binding)
            if verify_current
            else None
        )
        _verify_install_release_canonical_binding(home, binding)
        if verify_current:
            assert current_snapshot is not None
            if _current_release_binding_snapshot(home, binding) != current_snapshot:
                raise SyncError(
                    f"current release changed during final validation for owner "
                    f"{binding.owner}"
                )
        _verify_install_release_canonical_binding(home, binding)
    except (OSError, SyncError) as error:
        raise SyncError(
            f"release tree changed {phase}; raced release "
            f"{binding.owner}@{binding.sha} was left in place"
        ) from error


def _open_install_release_binding(
    home: Path,
    owner: str,
    sha: str,
    expectation: ReleaseTreeExpectation,
) -> InstallReleaseBinding:
    expected_identity, expected_directory_identity = expectation
    releases_root = _releases_root(home, owner)
    releases_fd = _open_directory_beneath(home, releases_root)
    release_fd = -1
    try:
        release_fd = os.open(
            sha,
            _source_directory_flags(),
            dir_fd=releases_fd,
        )
        binding = InstallReleaseBinding(
            owner=owner,
            sha=sha,
            expected_identity=expected_identity,
            expected_directory_identity=expected_directory_identity,
            releases_root=releases_root,
            releases_fd=releases_fd,
            release_fd=release_fd,
        )
        _verify_install_release_binding(
            home,
            binding,
            phase="while binding staged release",
            verify_current=False,
        )
        return binding
    except BaseException:
        if release_fd >= 0:
            _close_fd_quietly(release_fd)
        _close_fd_quietly(releases_fd)
        raise


def _close_install_release_bindings(
    releases: list[InstallReleaseBinding],
) -> None:
    for binding in releases:
        if binding.release_fd >= 0:
            _close_fd_quietly(binding.release_fd)
            binding.release_fd = -1
        if binding.releases_fd >= 0:
            _close_fd_quietly(binding.releases_fd)
            binding.releases_fd = -1


def _open_active_release_bindings(
    home: Path,
    active: dict[str, ActiveReleaseExpectation],
) -> dict[str, InstallReleaseBinding]:
    bindings: dict[str, InstallReleaseBinding] = {}
    try:
        for owner, release in sorted(active.items()):
            bindings[owner] = _open_install_release_binding(
                home,
                owner,
                release.sha,
                release.expectation,
            )
        return bindings
    except BaseException:
        _close_install_release_bindings(list(bindings.values()))
        raise


def _verify_install_release_identities(
    home: Path,
    releases: list[InstallReleaseBinding],
    *,
    phase: str,
    verify_current: bool,
) -> None:
    for binding in releases:
        _verify_install_release_binding(
            home,
            binding,
            phase=phase,
            verify_current=verify_current,
        )


def _verify_install_release_bindings_lightweight(
    home: Path,
    releases: list[InstallReleaseBinding],
    *,
    phase: str,
    verify_current: bool,
) -> None:
    for binding in releases:
        _verify_install_release_binding_lightweight(
            home,
            binding,
            phase=phase,
            verify_current=verify_current,
        )


def _owner_shas_from_bound_current_releases(
    home: Path,
    next_manifests: dict[str, ManifestData],
    expected_shas: dict[str, str],
    bindings: dict[str, InstallReleaseBinding],
    *,
    phase: str,
) -> dict[str, str]:
    next_owners = set(next_manifests)
    owner_shas = {
        owner: expected_shas[owner]
        for owner in sorted(next_owners)
        if owner in expected_shas
    }
    if set(owner_shas) != next_owners:
        missing = next_owners.difference(owner_shas)
        raise SyncError(
            "trusted release SHA is missing for owner(s): "
            + ", ".join(sorted(missing))
        )
    if set(bindings) != next_owners:
        missing = next_owners.difference(bindings)
        unexpected = set(bindings).difference(next_owners)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected " + ", ".join(sorted(unexpected)))
        raise SyncError("current release binding owner mismatch: " + "; ".join(details))
    for owner in sorted(next_owners):
        binding = bindings[owner]
        expected_sha = owner_shas[owner]
        if binding.owner != owner or binding.sha != expected_sha:
            raise SyncError(
                f"current release binding mismatch for owner {owner}: "
                f"expected {expected_sha}, got {binding.owner}@{binding.sha}"
            )
        try:
            _verify_install_release_binding_lightweight(
                home,
                binding,
                phase=phase,
                verify_current=True,
            )
        except SyncError as error:
            raise SyncError(
                f"current release changed {phase} for owner {owner}"
            ) from error
    return owner_shas


def _normalize_install_releases(
    releases: list[
        tuple[Path, str, ManifestData]
        | tuple[
            Path,
            str,
            ManifestData,
            ReleaseTreeExpectation | None,
        ]
    ],
) -> list[
    tuple[Path, str, ManifestData, ReleaseTreeExpectation | None]
]:
    normalized: list[
        tuple[Path, str, ManifestData, ReleaseTreeExpectation | None]
    ] = []
    for release in releases:
        if len(release) == 3:
            source_root, sha, manifest = release
            source_expectation = None
        elif len(release) == 4:
            source_root, sha, manifest, source_expectation = release
        else:
            raise SyncError("install release tuple must contain three or four values")
        sha = _validate_release_sha(sha)
        normalized.append((source_root, sha, manifest, source_expectation))
    return normalized


def _resolve_install_release_expectations(
    releases: list[
        tuple[Path, str, ManifestData, ReleaseTreeExpectation | None]
    ],
) -> list[tuple[Path, str, ManifestData, ReleaseTreeExpectation]]:
    resolved: list[tuple[Path, str, ManifestData, ReleaseTreeExpectation]] = []
    for source_root, sha, manifest, source_expectation in releases:
        if source_expectation is None:
            source_expectation = _source_release_identity(source_root, manifest)
        else:
            source_expectation = _source_release_identity(
                source_root,
                manifest,
                source_expectation,
            )
        resolved.append((source_root, sha, manifest, source_expectation))
    return resolved


def _preflight_pending_recovery(home: Path, *, dry_run: bool) -> bool:
    home = home.expanduser()
    loaded_state, initial_state_snapshot = _load_managed_state_with_snapshot(home)
    (
        _loaded_state,
        _initial_state_snapshot,
        observed_pending_transaction,
    ) = _recover_pending_link_transaction(
        home,
        loaded_state,
        initial_state_snapshot,
        dry_run=True,
    )
    if not observed_pending_transaction:
        return False
    if dry_run:
        print("would recover pending personal sync transaction under the install lock")
        return True
    with installation_lock(home):
        loaded_state, initial_state_snapshot = _load_managed_state_with_snapshot(home)
        (
            _loaded_state,
            _initial_state_snapshot,
            recovered_pending_transaction,
        ) = _recover_pending_link_transaction(
            home,
            loaded_state,
            initial_state_snapshot,
            dry_run=False,
        )
    return recovered_pending_transaction


def _install_release_set_unlocked(
    home: Path,
    releases: list[
        tuple[Path, str, ManifestData]
        | tuple[
            Path,
            str,
            ManifestData,
            ReleaseTreeExpectation | None,
        ]
    ],
    *,
    dry_run: bool,
    allow_cross_owner: bool,
    preflight_only: bool = False,
) -> None:
    releases = _normalize_install_releases(releases)
    home = home.expanduser()
    loaded_state, initial_state_snapshot = _load_managed_state_with_snapshot(home)
    (
        loaded_state,
        initial_state_snapshot,
        recovered_pending_transaction,
    ) = _recover_pending_link_transaction(
        home,
        loaded_state,
        initial_state_snapshot,
        dry_run=dry_run or preflight_only,
    )
    if recovered_pending_transaction and (dry_run or preflight_only):
        if not preflight_only:
            print(
                "would recover pending personal sync transaction under the install lock"
            )
        return
    if not dry_run and not preflight_only:
        _try_cleanup_ready_pending_batches(home)
    _verify_managed_state_current_claims(home, loaded_state)
    current_manifests = _installed_manifests(home)
    next_manifests = dict(current_manifests)
    for _source_root, _sha, manifest, _source_expectation in releases:
        next_manifests[manifest.owner] = manifest
    incoming_shas = {
        manifest.owner: sha
        for _source_root, sha, manifest, _source_expectation in releases
    }
    _validate_install_target_portability(current_manifests, next_manifests)
    releases = _resolve_install_release_expectations(releases)
    active_expectations = _capture_active_release_expectations(
        home,
        current_manifests,
    )
    expected_next_shas = {
        owner: expectation.sha
        for owner, expectation in active_expectations.items()
        if owner in next_manifests
    }
    expected_next_shas.update(
        {
            owner: sha
            for owner, sha in incoming_shas.items()
            if owner in next_manifests
        }
    )
    _validate_planned_overlay_base_release_shas(
        next_manifests,
        expected_next_shas.get(PUBLIC_OWNER),
    )
    public_manifest = next_manifests.get(PUBLIC_OWNER)
    if public_manifest is None:
        if len(next_manifests) != 1:
            raise SyncError("overlay installation requires an installed public base")
        only_manifest = next(iter(next_manifests.values()))
        if only_manifest.owner != PUBLIC_OWNER:
            raise SyncError("overlay installation requires an installed public base")
        public_manifest = only_manifest
    overlay_manifests = [
        manifest
        for owner, manifest in sorted(next_manifests.items())
        if owner != PUBLIC_OWNER
    ]
    desired_entries = _combine_entries(public_manifest.entries, overlay_manifests)
    removed_links = _combine_removed_links(list(next_manifests.values()))
    _validate_active_replacements(
        home,
        current_manifests,
        next_manifests,
        desired_entries,
    )
    previous_entries = [
        entry for manifest in current_manifests.values() for entry in manifest.entries
    ]
    bootstrap_history = (
        not initial_state_snapshot.exists and not recovered_pending_transaction
    )
    state = _refresh_managed_state_from_current(
        home,
        loaded_state,
        bootstrap_history=bootstrap_history,
    )
    baseline_link_snapshots = _capture_managed_state_link_snapshots(home, state)
    actions = _plan_reconciliation(
        home,
        desired_entries,
        previous_entries,
        removed_links,
        state,
        allow_cross_owner=allow_cross_owner,
    )
    required_replacements = _required_replacements_for_removals(
        home,
        actions,
        removed_links,
        desired_entries,
    )
    managed_targets = _managed_targets_after_reconciliation(home, state, actions)

    for source_root, sha, manifest, source_expectation in releases:
        _ensure_current_can_switch(home, manifest.owner)
        release_dir = _releases_root(home, manifest.owner) / sha
        if _path_exists_or_is_link(release_dir):
            _require_existing_release_matches_source(
                source_root,
                home,
                manifest.owner,
                sha,
                manifest,
                source_expectation,
            )
        if dry_run and not preflight_only:
            print(f"would install release {sha} into {release_dir}")
            _switch_current(home, sha, manifest.owner, dry_run=True)

    capacity_current_actions = _plan_current_switch_capacity_actions(
        home,
        releases,
    )
    planned_next_state = _planned_committed_state(
        home,
        desired_entries,
        expected_next_shas,
        managed_targets,
    )
    if not _canonical_managed_state_matches_snapshot(home, initial_state_snapshot):
        raise SyncError("managed state changed before install capacity validation")
    capacity_state_before = _managed_state_value_from_snapshot(
        home,
        initial_state_snapshot,
    )
    _build_pending_link_capacity_plan(
        home,
        [("current", capacity_current_actions), ("managed", actions)],
        initial_state_snapshot,
        capacity_state_before,
        state,
        planned_next_state,
        required_replacements_by_scope={
            "managed": required_replacements,
        },
    )

    if preflight_only:
        return
    if dry_run:
        _apply_reconcile_actions(
            home,
            actions,
            dry_run=True,
            required_replacements=required_replacements,
        )
        if not actions:
            print("all managed symlinks already point at current")
        return

    active_bindings = _open_active_release_bindings(home, active_expectations)
    held_bindings = list(active_bindings.values())
    staged_releases: list[InstallReleaseBinding] = []
    try:
        for source_root, sha, manifest, source_expectation in releases:
            release_dir = _releases_root(home, manifest.owner) / sha
            already_present = release_dir.exists()
            binding = _stage_release_tree_for_install(
                source_root,
                home,
                sha,
                manifest,
                source_expectation,
            )
            active_binding = active_bindings.get(manifest.owner)
            if active_binding is not None and active_binding.sha == sha:
                _close_install_release_bindings([binding])
            else:
                staged_releases.append(binding)
                held_bindings.append(binding)
            if already_present:
                print(f"release already present: {release_dir}")
            else:
                print(f"installed release tree: {release_dir}")
        current_actions = _plan_current_switch_actions(home, releases)
    except BaseException:
        _close_install_release_bindings(held_bindings)
        raise
    next_current_bindings = dict(active_bindings)
    next_current_bindings.update(
        {binding.owner: binding for binding in staged_releases}
    )
    active_still_current = [
        binding
        for owner, binding in active_bindings.items()
        if incoming_shas.get(owner, binding.sha) == binding.sha
    ]
    active_replaced = [
        binding
        for owner, binding in active_bindings.items()
        if incoming_shas.get(owner, binding.sha) != binding.sha
    ]
    pending_batch: PendingLinkBatch | None = None
    current_transaction: ReconcileTransaction | None = None
    link_transaction: ReconcileTransaction | None = None
    state_transaction: ManagedStateFileTransaction | None = None
    state_committed = False
    try:
        initial_state_snapshot = _bind_managed_state_parent_for_pending_staging(
            home,
            initial_state_snapshot,
            loaded_state,
        )
        exact_noop = (
            not current_actions
            and not actions
            and planned_next_state == loaded_state
            and initial_state_snapshot.exists
            and initial_state_snapshot.mode == 0o600
            and initial_state_snapshot.payload
            == _managed_state_bytes(planned_next_state)
        )
        if exact_noop:
            try:
                _verify_install_release_identities(
                    home,
                    list(active_bindings.values()),
                    phase="before activation",
                    verify_current=True,
                )
                _verify_desired_entries(home, desired_entries)
                _verify_install_release_identities(
                    home,
                    active_still_current,
                    phase="during final managed-state validation",
                    verify_current=True,
                )
                _verify_desired_entries(home, desired_entries)
                for manifest in overlay_manifests:
                    issues = _collect_overlay_issues(home, manifest.owner)
                    if issues:
                        raise SyncError(
                            "overlay no-op verification failed with "
                            f"{len(issues)} issue(s)"
                        )
            finally:
                _close_install_release_bindings(held_bindings)
            print("all managed symlinks already point at current")
            return
        pending_batch = _stage_pending_link_batch(
            home,
            [("current", current_actions), ("managed", actions)],
            desired_entries,
            expected_next_shas,
            initial_state_snapshot,
            state,
            planned_next_state,
            required_replacements_by_scope={
                "managed": required_replacements,
            },
        )
        _publish_pending_link_pointer(home, pending_batch)
        _verify_install_release_identities(
            home,
            list(active_bindings.values()),
            phase="before activation",
            verify_current=True,
        )
        _verify_install_release_identities(
            home,
            staged_releases,
            phase="before activation",
            verify_current=False,
        )
        current_transaction = ReconcileTransaction(
            batch_root=pending_batch.batch_root,
            mutations=[],
        )
        _apply_reconcile_actions(
            home,
            current_actions,
            dry_run=False,
            pending_batch=pending_batch,
            pending_scope="current",
            batch_root=pending_batch.batch_root,
            transaction=current_transaction,
        )
        link_transaction = ReconcileTransaction(
            batch_root=pending_batch.batch_root,
            mutations=[],
        )
        _apply_reconcile_actions(
            home,
            actions,
            dry_run=False,
            required_replacements=required_replacements,
            pending_batch=pending_batch,
            pending_scope="managed",
            batch_root=pending_batch.batch_root,
            transaction=link_transaction,
        )
        _verify_desired_entries(home, desired_entries)
        for manifest in overlay_manifests:
            issues = _collect_overlay_issues(home, manifest.owner)
            if issues:
                for issue in issues:
                    print(f"overlay issue: {issue}")
                raise SyncError(
                    f"overlay verification failed with {len(issues)} issue(s)"
                )
        owner_shas = _owner_shas_from_bound_current_releases(
            home,
            next_manifests,
            expected_next_shas,
            next_current_bindings,
            phase="before managed-state publication",
        )
        next_state = _committed_state(
            home,
            desired_entries,
            owner_shas,
            managed_targets,
        )
        if next_state != planned_next_state:
            raise SyncError("observed managed state differs from the pending plan")
        managed_link_snapshots = _trusted_managed_link_snapshots_for_state(
            home,
            next_state,
            baseline_link_snapshots,
            link_transaction,
        )
        _verify_install_release_bindings_lightweight(
            home,
            active_still_current,
            phase="during final managed-state validation",
            verify_current=True,
        )
        _verify_install_release_bindings_lightweight(
            home,
            active_replaced,
            phase="during final managed-state validation",
            verify_current=False,
        )
        _verify_install_release_bindings_lightweight(
            home,
            staged_releases,
            phase="during final managed-state validation",
            verify_current=True,
        )
        _verify_desired_entries(home, desired_entries)
        _verify_managed_link_snapshots(home, next_state, managed_link_snapshots)
        state_transaction = _prepare_pending_managed_state_transaction(
            home,
            pending_batch,
            next_state,
        )
        _verify_install_release_identities(
            home,
            active_still_current,
            phase="during final managed-state validation",
            verify_current=True,
        )
        _verify_install_release_identities(
            home,
            active_replaced,
            phase="during final managed-state validation",
            verify_current=False,
        )
        _verify_install_release_identities(
            home,
            staged_releases,
            phase="during final managed-state validation",
            verify_current=True,
        )
        _verify_desired_entries(home, desired_entries)
        _verify_managed_link_snapshots(home, next_state, managed_link_snapshots)
        _write_managed_state(home, next_state, state_transaction)
        _verify_published_state_transaction(home, state_transaction)
        _verify_install_release_identities(
            home,
            active_still_current,
            phase="after managed-state publication",
            verify_current=True,
        )
        _verify_install_release_identities(
            home,
            active_replaced,
            phase="after managed-state publication",
            verify_current=False,
        )
        _verify_install_release_identities(
            home,
            staged_releases,
            phase="after managed-state publication",
            verify_current=True,
        )
        _verify_desired_entries(home, desired_entries)
        _verify_managed_link_snapshots(home, next_state, managed_link_snapshots)
        _verify_committed_pending_link_records(home, pending_batch)
        _verify_published_state_transaction(home, state_transaction)
        _publish_pending_commit_marker(home, pending_batch)
        state_committed = True
        _mark_pending_batch_cleanup_ready(home, pending_batch)
        _clear_pending_link_pointer(home, pending_batch, phase="after")
    except BaseException as error:
        if (
            not state_committed
            and pending_batch is not None
            and pending_batch.pointer_snapshot is not None
        ):
            try:
                state_committed = _pending_commit_decision(home, pending_batch)
            except (OSError, SyncError) as marker_error:
                _close_install_release_bindings(held_bindings)
                raise SyncError(
                    "installation failed with an ambiguous commit marker; the "
                    "pending transaction was retained: "
                    f"{marker_error}"
                ) from error
        if state_committed:
            _close_install_release_bindings(held_bindings)
            raise SyncError(
                "installation committed managed state but finalization failed; "
                "the pending transaction was retained for exact recovery"
            ) from error
        rollback_errors: list[str] = []
        try:
            _restore_managed_state_file(home, state_transaction)
        except (OSError, SyncError) as rollback_error:
            rollback_errors.append(f"state: {rollback_error}")
        try:
            _rollback_reconcile_transaction(home, link_transaction)
        except (OSError, SyncError) as rollback_error:
            rollback_errors.append(f"links: {rollback_error}")
        try:
            _rollback_reconcile_transaction(home, current_transaction)
        except (OSError, SyncError) as rollback_error:
            rollback_errors.append(f"current: {rollback_error}")
        if (
            pending_batch is not None
            and pending_batch.pointer_snapshot is not None
            and not rollback_errors
        ):
            try:
                _clear_pending_link_pointer(home, pending_batch, phase="before")
            except (OSError, SyncError) as rollback_error:
                rollback_errors.append(f"pending pointer: {rollback_error}")
        if rollback_errors:
            _close_install_release_bindings(held_bindings)
            raise SyncError(
                f"installation failed: {error}; rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from error
        _close_install_release_bindings(held_bindings)
        raise
    try:
        _commit_managed_state_transaction(state_transaction)
        _commit_reconcile_transaction(link_transaction)
        _commit_reconcile_transaction(current_transaction)
        assert pending_batch is not None
        _try_cleanup_committed_pending_batch(home, pending_batch)
    finally:
        _close_install_release_bindings(held_bindings)
    if not actions:
        print("all managed symlinks already point at current")


def install_release_tree(
    source_root: Path,
    home: Path,
    sha: str,
    *,
    dry_run: bool,
    release_expectation: ReleaseTreeExpectation | None = None,
) -> None:
    sha = _validate_release_sha(sha)
    home = home.expanduser()
    if _preflight_pending_recovery(home, dry_run=dry_run) and dry_run:
        return
    if release_expectation is None:
        release_expectation = _source_release_identity(source_root, None)
    else:
        release_expectation = _source_release_identity(
            source_root,
            release_expectation[0][1],
            release_expectation,
        )
    manifest = release_expectation[0][1]
    releases = [(source_root, sha, manifest, release_expectation)]
    if dry_run:
        _install_release_set_unlocked(
            home,
            releases,
            dry_run=True,
            allow_cross_owner=manifest.owner != PUBLIC_OWNER,
        )
        return
    _install_release_set_unlocked(
        home,
        releases,
        dry_run=True,
        allow_cross_owner=manifest.owner != PUBLIC_OWNER,
        preflight_only=True,
    )
    with installation_lock(home):
        _install_release_set_unlocked(
            home,
            releases,
            dry_run=False,
            allow_cross_owner=manifest.owner != PUBLIC_OWNER,
        )


def _stage_release_tree_for_install(
    source_root: Path,
    home: Path,
    sha: str,
    manifest: ManifestData,
    source_expectation: ReleaseTreeExpectation | None = None,
) -> InstallReleaseBinding:
    sha = _validate_release_sha(sha)
    if source_expectation is None:
        source_expectation = _source_release_identity(source_root, manifest)
    owner = _entries_owner(manifest.entries)
    _ensure_install_roots(home, owner)
    release_dir = _releases_root(home, owner) / sha
    if _path_exists_or_is_link(release_dir):
        expectation = _require_existing_release_matches_source(
            source_root,
            home,
            owner,
            sha,
            manifest,
            source_expectation,
        )
    else:
        expectation = _copy_release_tree(
            source_root,
            release_dir,
            home,
            manifest,
            source_expectation,
        )
    return _open_install_release_binding(
        home,
        owner,
        sha,
        expectation,
    )


def _run_gh_process(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as error:
        raise SyncError(
            "GitHub CLI `gh` is not available; install it or make sure it is on PATH"
        ) from error


def _run_gh_json(args: list[str]) -> Any:
    completed = _run_gh_process(args)
    if completed.returncode != 0:
        raise SyncError(completed.stderr.strip() or "gh command failed")
    try:
        return json.loads(
            completed.stdout,
            parse_int=_bounded_json_integer,
        )
    except json.JSONDecodeError as error:
        raise SyncError(f"gh returned invalid JSON: {error}") from error


def _run_gh_json_stream(args: list[str]) -> list[Any]:
    completed = _run_gh_process(args)
    if completed.returncode != 0:
        raise SyncError(completed.stderr.strip() or "gh command failed")
    decoder = json.JSONDecoder()
    values: list[Any] = []
    text = completed.stdout
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as error:
            raise SyncError(f"gh returned invalid paginated JSON: {error}") from error
        values.append(value)
    return values


def _run_gh(args: list[str]) -> None:
    completed = _run_gh_process(args)
    if completed.returncode != 0:
        raise SyncError(completed.stderr.strip() or "gh command failed")


def find_latest_release(repo: str) -> dict[str, Any]:
    release_pages = _run_gh_json_stream(
        [
            "api",
            f"repos/{repo}/releases?per_page=100",
            "--paginate",
        ]
    )
    if release_pages is None:
        raise SyncError(f"no {TAG_PREFIX} release found in {repo}")
    if not isinstance(release_pages, list):
        raise SyncError("gh api releases returned an unexpected payload")
    for page in release_pages:
        if not isinstance(page, list):
            raise SyncError("gh api releases returned an unexpected payload")
        for release_data in page:
            if not isinstance(release_data, dict):
                continue
            tag_name = release_data.get("tag_name") or release_data.get("tagName")
            if (
                isinstance(tag_name, str)
                and tag_name.startswith(TAG_PREFIX)
                and not release_data.get("draft", False)
                and not release_data.get("prerelease", False)
            ):
                normalized = _normalize_release(release_data)
                select_release_assets(normalized)
                return normalized
    raise SyncError(f"no {TAG_PREFIX} release found in {repo}")


def find_release_by_asset_sha(repo: str, sha: str) -> dict[str, Any]:
    sha = _validate_release_sha(sha)
    release_pages = _run_gh_json_stream(
        [
            "api",
            f"repos/{repo}/releases?per_page=100",
            "--paginate",
        ]
    )
    if not isinstance(release_pages, list):
        raise SyncError("gh api releases returned an unexpected payload")
    for page in release_pages:
        if not isinstance(page, list):
            raise SyncError("gh api releases returned an unexpected payload")
        for release_data in page:
            if not isinstance(release_data, dict):
                continue
            if release_data.get("draft", False) or release_data.get("prerelease", False):
                continue
            tag_name = release_data.get("tag_name") or release_data.get("tagName")
            if not isinstance(tag_name, str) or not tag_name.startswith(TAG_PREFIX):
                continue
            normalized = _normalize_release(release_data)
            if not _release_mentions_asset_sha(normalized, sha):
                continue
            assets = select_release_assets(normalized)
            if assets.sha == sha:
                return normalized
    raise SyncError(f"no {TAG_PREFIX} release with asset SHA {sha} found in {repo}")


def _terminate_gh_download_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    except OSError:
        return


def _gh_download_error(stderr_file: Any) -> str:
    stderr_file.flush()
    stderr_file.seek(0)
    payload = stderr_file.read(64 * 1024 + 1)
    truncated = len(payload) > 64 * 1024
    message = payload[: 64 * 1024].decode("utf-8", errors="replace").strip()
    if truncated:
        message = f"{message}\n[stderr truncated]" if message else "[stderr truncated]"
    return message


def _isolate_download_entry_for_cleanup(
    directory_fd: int,
    name: str,
    bound_fd: int,
    *,
    label: str,
) -> None:
    retained_name: str | None = None
    try:
        for candidate in _temporary_archive_entry_names("retained-download"):
            try:
                _rename_noreplace_at(
                    directory_fd,
                    name,
                    directory_fd,
                    candidate,
                )
            except FileExistsError:
                continue
            except FileNotFoundError:
                return
            retained_name = candidate
            break
    except OSError as error:
        raise SyncError(f"failed to isolate {label}: {error}") from error
    if retained_name is None:
        raise SyncError(f"failed to isolate {label}")
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise SyncError(
            f"failed to persist isolated {label}; left as {retained_name}"
        ) from error
    if not _archive_entry_matches_fd(directory_fd, retained_name, bound_fd):
        raise SyncError(
            f"{label} changed during cleanup; preserved as {retained_name}"
        )
    try:
        os.unlink(retained_name, dir_fd=directory_fd)
    except OSError as error:
        raise SyncError(
            f"failed to remove {label}; preserved as {retained_name}"
        ) from error
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise SyncError(f"failed to persist removal of {label}") from error


def _download_release_asset(
    repo: str,
    asset_name: str,
    asset_id: int,
    expected_size: int,
    maximum_bytes: int,
    destination: Path,
) -> None:
    _validated_release_asset_metadata(
        {"id": asset_id, "size": expected_size},
        asset_name,
        maximum_bytes=maximum_bytes,
    )
    if Path(asset_name).name != asset_name or asset_name in {"", ".", ".."}:
        raise SyncError(f"release asset has an unsafe name: {asset_name}")
    destination_path = destination / asset_name
    destination_fd: int | None = None
    partial_name: str | None = None
    partial_fd = -1
    destination_linked = False
    completed = False
    try:
        destination_fd = os.open(destination, _archive_directory_open_flags())
    except OSError as error:
        raise SyncError(
            f"failed to open release download directory {destination}: {error}"
        ) from error
    process: subprocess.Popen[bytes] | None = None
    stdout: Any | None = None
    try:
        if not _archive_path_matches_fd(destination, destination_fd):
            raise SyncError(f"release download directory changed: {destination}")
        try:
            os.stat(asset_name, dir_fd=destination_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SyncError(
                f"refusing to overwrite downloaded release asset: {destination_path}"
            )

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        for candidate in _temporary_archive_entry_names("download"):
            try:
                partial_fd = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=destination_fd,
                )
            except FileExistsError:
                continue
            partial_name = candidate
            break
        if partial_name is None or partial_fd < 0:
            raise SyncError(
                f"failed to create partial download for release asset {asset_name}"
            )

        with tempfile.TemporaryFile(mode="w+b") as stderr_file:
            try:
                process = subprocess.Popen(
                    [
                        "gh",
                        "api",
                        f"repos/{repo}/releases/assets/{asset_id}",
                        "-H",
                        "Accept: application/octet-stream",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                )
            except OSError as error:
                raise SyncError(
                    "GitHub CLI `gh` is not available; install it or make sure it is on PATH"
                ) from error
            stdout = process.stdout
            if stdout is None:
                raise SyncError(f"gh did not provide a download stream for {asset_name}")

            received = 0
            while True:
                read_size = min(64 * 1024, expected_size - received + 1)
                chunk = stdout.read(read_size)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size or received > maximum_bytes:
                    _terminate_gh_download_process(process)
                    raise SyncError(
                        f"downloaded release asset {asset_name} exceeds its "
                        f"advertised {expected_size} byte size"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(partial_fd, view)
                    if written <= 0:
                        raise SyncError(
                            f"failed to write downloaded release asset: {asset_name}"
                        )
                    view = view[written:]

            stdout.close()
            stdout = None
            returncode = process.wait()
            if returncode != 0:
                message = _gh_download_error(stderr_file)
                raise SyncError(message or f"gh failed to download release asset {asset_name}")
            if received != expected_size:
                raise SyncError(
                    f"downloaded release asset {asset_name} size mismatch: "
                    f"expected {expected_size}, got {received}"
                )

        actual_size = os.fstat(partial_fd).st_size
        if actual_size != expected_size:
            raise SyncError(
                f"downloaded release asset {asset_name} file size mismatch: "
                f"expected {expected_size}, got {actual_size}"
            )
        os.fsync(partial_fd)
        if not _archive_entry_matches_fd(
            destination_fd,
            partial_name,
            partial_fd,
        ):
            raise SyncError(
                f"partial release asset changed before publication: {asset_name}"
            )
        try:
            os.link(
                partial_name,
                asset_name,
                src_dir_fd=destination_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise SyncError(
                f"refusing to overwrite downloaded release asset: {destination_path}"
            ) from error
        destination_linked = True
        if not _archive_entry_matches_fd(
            destination_fd,
            asset_name,
            partial_fd,
        ):
            raise SyncError(
                f"downloaded release asset changed during publication: {asset_name}"
            )
        _isolate_download_entry_for_cleanup(
            destination_fd,
            partial_name,
            partial_fd,
            label="partial release asset",
        )
        partial_name = None
        if not _archive_path_matches_fd(destination, destination_fd):
            raise SyncError(f"release download directory changed: {destination}")
        if not _archive_entry_matches_fd(
            destination_fd,
            asset_name,
            partial_fd,
        ):
            raise SyncError(
                f"downloaded release asset changed after publication: {asset_name}"
            )
        completed = True
    except SyncError:
        raise
    except OSError as error:
        raise SyncError(f"failed to download release asset {asset_name}: {error}") from error
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_errors: list[SyncError] = []
        if process is not None:
            _terminate_gh_download_process(process)
        if stdout is not None:
            try:
                stdout.close()
            except (OSError, ValueError):
                pass
        try:
            if destination_fd is not None:
                if partial_fd >= 0 and not completed and destination_linked:
                    try:
                        _isolate_download_entry_for_cleanup(
                            destination_fd,
                            asset_name,
                            partial_fd,
                            label="published release asset",
                        )
                    except (OSError, SyncError) as error:
                        cleanup_errors.append(
                            error
                            if isinstance(error, SyncError)
                            else SyncError(
                                f"failed to clean published release asset: {error}"
                            )
                        )
                if partial_fd >= 0 and partial_name is not None:
                    try:
                        _isolate_download_entry_for_cleanup(
                            destination_fd,
                            partial_name,
                            partial_fd,
                            label="partial release asset",
                        )
                    except (OSError, SyncError) as error:
                        cleanup_errors.append(
                            error
                            if isinstance(error, SyncError)
                            else SyncError(
                                f"failed to clean partial release asset: {error}"
                            )
                        )
        finally:
            if destination_fd is not None:
                _close_fd_quietly(destination_fd)
            if partial_fd >= 0:
                _close_fd_quietly(partial_fd)
        if cleanup_errors and not active_error:
            raise cleanup_errors[0]


def download_release_assets(repo: str, assets: ReleaseAssets, destination: Path) -> None:
    downloads = (
        (
            assets.archive_name,
            assets.archive_id,
            assets.archive_size,
            MAX_ARCHIVE_COMPRESSED_BYTES,
        ),
        (
            assets.checksum_name,
            assets.checksum_id,
            assets.checksum_size,
            MAX_ARCHIVE_CHECKSUM_BYTES,
        ),
    )
    asset_ids: set[int] = set()
    for asset_name, asset_id, asset_size, maximum_bytes in downloads:
        _validated_release_asset_metadata(
            {"id": asset_id, "size": asset_size},
            asset_name,
            maximum_bytes=maximum_bytes,
        )
        if asset_id in asset_ids:
            raise SyncError("release archive and checksum must have distinct GitHub asset ids")
        asset_ids.add(asset_id)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SyncError(f"failed to create release download directory: {destination}") from error
    for asset_name, asset_id, asset_size, maximum_bytes in downloads:
        _download_release_asset(
            repo,
            asset_name,
            asset_id,
            asset_size,
            maximum_bytes,
            destination,
        )


def download_and_extract_release(
    repo: str,
    destination: Path,
    *,
    sha: str | None = None,
) -> DownloadedRelease:
    release = find_release_by_asset_sha(repo, sha) if sha is not None else find_latest_release(repo)
    assets = select_release_assets(release)
    destination.mkdir(parents=True, exist_ok=True)
    download_release_assets(repo, assets, destination)
    archive_path = destination / assets.archive_name
    checksum_path = destination / assets.checksum_name
    extract_root = destination / "extract"
    release_root, release_expectation = verify_and_extract_archive(
        archive_path,
        checksum_path,
        extract_root,
    )
    return DownloadedRelease(
        repo=repo,
        assets=assets,
        release_root=release_root,
        release_expectation=release_expectation,
    )


def install_from_github(repo: str, home: Path, *, dry_run: bool) -> None:
    home = home.expanduser()
    if _preflight_pending_recovery(home, dry_run=dry_run) and dry_run:
        return
    with tempfile.TemporaryDirectory(prefix="codex-personal-sync.") as temp_dir_raw:
        release = download_and_extract_release(repo, Path(temp_dir_raw))
        install_release_tree(
            release.release_root,
            home,
            release.assets.sha,
            dry_run=dry_run,
            release_expectation=release.release_expectation,
        )


def _validate_release_manifest_owner(
    release_root: Path,
    expected_owner: str,
    release_expectation: ReleaseTreeExpectation | None = None,
) -> ManifestData:
    expected_owner = _validate_owner(expected_owner)
    if release_expectation is None:
        release_expectation = _source_release_identity(release_root, None)
    else:
        release_expectation = _source_release_identity(
            release_root,
            release_expectation[0][1],
            release_expectation,
        )
    manifest = release_expectation[0][1]
    if manifest.owner != expected_owner:
        raise SyncError(
            f"release owner mismatch: expected {expected_owner}, got {manifest.owner}"
        )
    return manifest


def _validate_release_owner(release_root: Path, expected_owner: str) -> list[LinkEntry]:
    return _validate_release_manifest_owner(release_root, expected_owner).entries


def install_private_from_github(
    repo: str,
    home: Path,
    *,
    base_repo: str,
    owner: str,
    dry_run: bool,
) -> None:
    home = home.expanduser()
    if _preflight_pending_recovery(home, dry_run=dry_run) and dry_run:
        return
    owner = _validate_owner(owner)
    if owner == PUBLIC_OWNER:
        raise SyncError("install-private owner must not be public")

    with tempfile.TemporaryDirectory(prefix="codex-personal-sync-private.") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        overlay_release = download_and_extract_release(repo, temp_dir / "overlay")
        overlay_manifest = _validate_release_manifest_owner(
            overlay_release.release_root,
            owner,
            overlay_release.release_expectation,
        )
        base_spec = _load_base_release_spec(overlay_manifest, base_repo)
        base_release = download_and_extract_release(
            base_spec.repo,
            temp_dir / "base",
            sha=base_spec.sha,
        )
        base_manifest = _validate_release_manifest_owner(
            base_release.release_root,
            PUBLIC_OWNER,
            base_release.release_expectation,
        )
        releases = [
            (
                base_release.release_root,
                base_release.assets.sha,
                base_manifest,
                base_release.release_expectation,
            ),
            (
                overlay_release.release_root,
                overlay_release.assets.sha,
                overlay_manifest,
                overlay_release.release_expectation,
            ),
        ]

        if dry_run:
            print(
                "would install private layered release: "
                f"base {base_spec.repo}@{base_release.assets.sha}, "
                f"overlay {repo}@{overlay_release.assets.sha}"
            )
            _install_release_set_unlocked(
                home,
                releases,
                dry_run=True,
                allow_cross_owner=True,
            )
            return

        with installation_lock(home):
            _install_release_set_unlocked(
                home,
                releases,
                dry_run=False,
                allow_cross_owner=True,
            )
        print(
            "private layered install ok: "
            f"base {base_spec.repo}@{base_release.assets.sha}, "
            f"overlay {repo}@{overlay_release.assets.sha}"
        )


def _current_sha(home: Path, owner: str = PUBLIC_OWNER) -> str | None:
    current = _current_link(home, owner)
    if not _ensure_safe_internal_parent(
        home,
        current,
        create=False,
        allow_missing=True,
    ):
        return None
    if not _path_exists_or_is_link(current):
        return None
    current_metadata = os.lstat(current)
    if not stat.S_ISLNK(current_metadata.st_mode):
        raise SyncError(f"refusing non-symlink current pointer: {current}")

    releases_root = _releases_root(home, owner)
    _ensure_safe_internal_directory(
        home,
        releases_root,
        create=False,
        allow_missing=False,
    )
    raw_target_text = os.readlink(current)
    raw_parts = raw_target_text.split("/")
    if len(raw_parts) != 2 or raw_parts[0] != "releases":
        raise SyncError(
            f"current pointer must use releases/<sha> for owner {owner}"
        )
    try:
        sha = _validate_release_sha(raw_parts[1])
    except SyncError as error:
        raise SyncError(
            f"current pointer has invalid release SHA for owner {owner}"
        ) from error
    release_dir = releases_root / sha
    try:
        resolved = release_dir.resolve(strict=True)
        resolved_releases_root = releases_root.resolve(strict=True)
        resolved_relative = resolved.relative_to(resolved_releases_root)
    except (OSError, ValueError) as error:
        raise SyncError(f"current pointer is invalid for owner {owner}: {current}") from error
    if resolved_relative.parts != (sha,):
        raise SyncError(
            f"current pointer must resolve exactly to a release directory for owner {owner}"
        )
    release_metadata = os.lstat(release_dir)
    if (
        stat.S_ISLNK(release_metadata.st_mode)
        or not stat.S_ISDIR(release_metadata.st_mode)
    ):
        raise SyncError(
            f"current pointer must reference a non-symlink release directory: {release_dir}"
        )
    try:
        current_metadata_after = os.lstat(current)
        raw_target_after = os.readlink(current)
        release_metadata_after = os.lstat(release_dir)
        resolved_after = release_dir.resolve(strict=True)
    except OSError as error:
        raise SyncError(
            f"current pointer changed during validation for owner {owner}"
        ) from error
    if (
        not stat.S_ISLNK(current_metadata_after.st_mode)
        or (current_metadata_after.st_dev, current_metadata_after.st_ino)
        != (current_metadata.st_dev, current_metadata.st_ino)
        or raw_target_after != raw_target_text
        or (release_metadata_after.st_dev, release_metadata_after.st_ino)
        != (release_metadata.st_dev, release_metadata.st_ino)
        or resolved_after != resolved
    ):
        raise SyncError(
            f"current pointer changed during validation for owner {owner}"
        )
    return sha


def status(home: Path, owner: str = PUBLIC_OWNER) -> None:
    home = home.expanduser()
    owner = _validate_owner(owner)
    sha = _current_sha(home, owner)
    if sha is None:
        print(f"{owner} is not installed under {_display_path(home)}")
        return
    release_root = _releases_root(home, owner) / sha
    entries = _load_installed_manifest_data(home, owner, sha).entries
    actions = plan_link_actions(home, entries)
    stale_removals = plan_stale_current_link_removals(home, entries)
    print(f"current owner: {owner}")
    print(f"current release: {sha}")
    print(f"release root: {release_root}")
    if actions:
        print(f"managed symlink drift: {len(actions)} update(s) needed")
        for action in actions:
            print(f"- {action.action}: {action.target} -> {action.link_target}")
    else:
        print("current manifest symlinks: ok")
    if stale_removals:
        print(f"stale managed symlinks: {len(stale_removals)}")
        for removal in stale_removals:
            print(f"- stale: {removal.target}")
    state_path = _state_path(home)
    if not _path_exists_or_is_link(state_path):
        print("managed link state: not initialized")
        return
    state = _load_managed_state(home)
    state_issues: list[str] = []
    if state.owners.get(owner) != sha:
        state_issues.append(
            f"release mismatch: state={state.owners.get(owner)}, current={sha}"
        )
    for record in state.links.values():
        if record.owner != owner:
            continue
        target = home / Path(*record.target.parts)
        if not target.is_symlink() or os.readlink(target) != record.link_target:
            state_issues.append(f"link mismatch: {target}")
    if state_issues:
        print(f"managed link state drift: {len(state_issues)} issue(s)")
        for issue in state_issues:
            print(f"- {issue}")
    else:
        print("managed link state: ok")


def _valid_release_dirs(home: Path, owner: str) -> list[Path]:
    releases_root = _releases_root(home, owner)
    releases: list[Path] = []
    releases_fd = _open_directory_beneath(home, releases_root)
    try:
        names = _directory_member_names(releases_fd)
        for name in names:
            if RELEASE_DIR_RE.fullmatch(name) is None:
                continue
            path = releases_root / name
            mode = os.stat(name, dir_fd=releases_fd, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise SyncError(f"refusing unsafe release directory: {path}")
            try:
                _load_installed_manifest_data(home, owner, name)
            except SyncError:
                continue
            releases.append(path)
        if _directory_member_names(releases_fd) != names:
            raise SyncError(f"release directory changed during validation: {releases_root}")
        if not _bound_directory_matches(home, releases_root, releases_fd):
            raise SyncError(f"release directory changed during validation: {releases_root}")
        return releases
    finally:
        _close_fd_quietly(releases_fd)


def _resolve_release_for_rollback(
    home: Path,
    to_sha: str | None,
    owner: str = PUBLIC_OWNER,
) -> str:
    releases_root = _releases_root(home, owner)
    if not _ensure_safe_internal_directory(
        home,
        releases_root,
        create=False,
        allow_missing=True,
    ):
        raise SyncError(f"release root is missing: {releases_root}")
    releases = _valid_release_dirs(home, owner)
    if to_sha:
        matches = [path.name for path in releases if path.name.startswith(to_sha)]
        if not matches:
            raise SyncError(f"no release matches {to_sha}")
        if len(matches) > 1:
            raise SyncError(f"release prefix is ambiguous: {to_sha}")
        return matches[0]

    current = _current_sha(home, owner)
    candidates = sorted(releases, key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.name != current:
            return candidate.name
    raise SyncError("no previous release is available")


def rollback(home: Path, to_sha: str | None, owner: str = PUBLIC_OWNER) -> None:
    home = home.expanduser()
    with installation_lock(home):
        loaded_state, initial_state_snapshot = _load_managed_state_with_snapshot(home)
        _recover_pending_link_transaction(
            home,
            loaded_state,
            initial_state_snapshot,
            dry_run=False,
        )
        owner = _validate_owner(owner)
        if owner != PUBLIC_OWNER:
            raise SyncError(
                "rollback currently supports only public releases; rerun install-private"
            )
        if not _ensure_safe_internal_directory(
            home,
            _releases_root(home, owner),
            create=False,
            allow_missing=True,
        ):
            raise SyncError(f"release root is missing: {_releases_root(home, owner)}")
        sha = _resolve_release_for_rollback(home, to_sha, owner)
        release_root = _releases_root(home, owner) / sha
        manifest = _load_installed_manifest_data(home, owner, sha)
        release_expectation = _installed_release_identity_and_directory_identity(
            home,
            owner,
            sha,
        )
        _install_release_set_unlocked(
            home,
            [(release_root, sha, manifest, release_expectation)],
            dry_run=False,
            allow_cross_owner=False,
        )


def _entries_by_target(entries: list[LinkEntry]) -> dict[PurePosixPath, LinkEntry]:
    return {entry.target: entry for entry in entries}


def _overlay_scan_parents(
    home: Path,
    owner: str,
    overlay_entries: list[LinkEntry],
    public_entries: list[LinkEntry],
) -> set[Path]:
    parents = _known_manifest_target_parents(home, overlay_entries, owner=owner)
    parents.update(_known_manifest_target_parents(home, public_entries, owner=PUBLIC_OWNER))
    return parents


def _collect_overlay_issues(home: Path, owner: str) -> list[str]:
    owner = _validate_owner(owner)
    if owner == PUBLIC_OWNER:
        raise SyncError("overlay owner must not be public")
    overlay_entries = current_release_entries(home, owner)
    public_entries = current_release_entries(home, PUBLIC_OWNER)
    if not overlay_entries:
        return [f"overlay {owner} is not installed"]

    public_by_target = _entries_by_target(public_entries)
    overlay_by_target = _entries_by_target(overlay_entries)
    overlay_targets = {_entry_target_path(home, entry) for entry in overlay_entries}
    issues: list[str] = []

    known_owners = {owner, PUBLIC_OWNER}
    for entry in overlay_entries:
        target = _entry_target_path(home, entry)
        live_target = _read_optional_symlink_target_beneath(home, target)
        if live_target is None:
            issues.append(f"missing overlay symlink: {target}")
            continue
        live_owner = _link_managed_owner(home, target, known_owners)
        if live_owner != owner:
            issues.append(f"overlay target is not owned by {owner}: {target}")
        if live_target != _desired_link_target(home, entry):
            issues.append(f"overlay target drift: {target}")
        public_entry = public_by_target.get(entry.target)
        if (
            public_entry is not None
            and not entry.override
            and entry.target not in OPTIONAL_PUBLIC_TARGETS
        ):
            issues.append(
                f"target also exists in public manifest but lacks override=true: {target}"
            )
        if (
            public_entry is None
            and entry.override
            and entry.target not in OPTIONAL_PUBLIC_TARGETS
        ):
            issues.append(f"override target has no public base target: {target}")

    for public_entry in public_entries:
        target = _entry_target_path(home, public_entry)
        if _read_optional_symlink_target_beneath(home, target) is None:
            continue
        live_owner = _link_managed_owner(home, target, known_owners)
        if live_owner != owner:
            continue
        overlay_entry = overlay_by_target.get(public_entry.target)
        if (
            public_entry.target not in OPTIONAL_PUBLIC_TARGETS
            and (overlay_entry is None or not overlay_entry.override)
        ):
            issues.append(f"public target is shadowed by undeclared overlay: {target}")

    for parent in sorted(_overlay_scan_parents(home, owner, overlay_entries, public_entries)):
        if not parent.is_dir():
            continue
        for candidate in parent.iterdir():
            if candidate in overlay_targets:
                continue
            if _read_optional_symlink_target_beneath(home, candidate) is None:
                continue
            if _link_managed_owner(home, candidate, known_owners) == owner:
                issues.append(f"private-owned symlink is not in overlay manifest: {candidate}")

    return issues


def verify_overlay(home: Path, owner: str) -> None:
    home = home.expanduser()
    issues = _collect_overlay_issues(home, owner)
    state_path = _state_path(home)
    if _path_exists_or_is_link(state_path):
        state = _load_managed_state(home)
        current_sha = _current_sha(home, owner)
        if state.owners.get(owner) != current_sha:
            issues.append(
                f"managed link state release mismatch for {owner}: "
                f"state={state.owners.get(owner)}, current={current_sha}"
            )
        for entry in current_release_entries(home, owner):
            record = state.links.get(entry.target)
            if record is None or record.owner != owner:
                issues.append(
                    f"managed link state is missing overlay target: "
                    f"{_entry_target_path(home, entry)}"
                )
    if issues:
        for issue in issues:
            print(f"overlay issue: {issue}")
        raise SyncError(f"overlay verification failed with {len(issues)} issue(s)")
    print(f"overlay verification ok: {owner}")


def uninstall_overlay(home: Path, owner: str, *, dry_run: bool) -> None:
    home = home.expanduser()
    owner = _validate_owner(owner)
    if owner == PUBLIC_OWNER:
        raise SyncError("refusing to uninstall public as an overlay")

    def apply_uninstall() -> None:
        loaded_state, initial_state_snapshot = _load_managed_state_with_snapshot(home)
        (
            loaded_state,
            initial_state_snapshot,
            recovered_pending_transaction,
        ) = _recover_pending_link_transaction(
            home,
            loaded_state,
            initial_state_snapshot,
            dry_run=dry_run,
        )
        if recovered_pending_transaction and dry_run:
            print("would recover pending personal sync transaction under the install lock")
            return
        if not dry_run:
            _try_cleanup_ready_pending_batches(home)
        _verify_managed_state_current_claims(home, loaded_state)
        # Uncommitted recovery returns the exact restored before-state snapshot.
        # If that state was absent, plan this uninstall like a clean legacy retry
        # so the restored links are claimed before their owner is retired.
        state = _refresh_managed_state_from_current(
            home,
            loaded_state,
            bootstrap_history=not initial_state_snapshot.exists,
        )
        baseline_link_snapshots = _capture_managed_state_link_snapshots(home, state)
        current_manifests = _installed_manifests(home)
        active_expectations = _capture_active_release_expectations(
            home,
            current_manifests,
        )
        outgoing_sha = state.owners.get(owner)
        outgoing_manifest = current_manifests.get(owner)
        if outgoing_manifest is None:
            if outgoing_sha is not None:
                outgoing_manifest = _load_installed_manifest_data(
                    home,
                    owner,
                    outgoing_sha,
                )
                if outgoing_manifest.owner != owner:
                    raise SyncError(
                        f"managed link state release owner mismatch: expected {owner}, "
                        f"got {outgoing_manifest.owner}"
                    )
                current_manifests[owner] = outgoing_manifest
        if outgoing_manifest is None:
            print(f"overlay {owner} is not installed")
            return
        if outgoing_sha is None:
            raise SyncError(f"managed link state release is missing for owner {owner}")
        next_manifests = {
            current_owner: manifest
            for current_owner, manifest in current_manifests.items()
            if current_owner != owner
        }
        public_manifest = next_manifests.get(PUBLIC_OWNER)
        if public_manifest is None:
            raise SyncError("overlay uninstall requires an installed public base")
        overlay_manifests = [
            manifest
            for current_owner, manifest in sorted(next_manifests.items())
            if current_owner != PUBLIC_OWNER
        ]
        desired_entries = _combine_entries(public_manifest.entries, overlay_manifests)
        _validate_active_replacements(
            home,
            current_manifests,
            next_manifests,
            desired_entries,
        )
        previous_entries = [
            entry for manifest in current_manifests.values() for entry in manifest.entries
        ]
        historical_removed_links = _combine_removed_links(
            list(current_manifests.values())
        )
        active_removed_links = _combine_removed_links(list(next_manifests.values()))
        actions = _plan_reconciliation(
            home,
            desired_entries,
            previous_entries,
            historical_removed_links,
            state,
            allow_cross_owner=True,
        )
        required_replacements = _required_replacements_for_removals(
            home,
            actions,
            active_removed_links,
            desired_entries,
        )
        managed_targets = _managed_targets_after_reconciliation(home, state, actions)
        current = _current_link(home, owner)
        if dry_run:
            _apply_reconcile_actions(
                home,
                actions,
                dry_run=True,
                required_replacements=required_replacements,
            )
            if _path_exists_or_is_link(current):
                print(f"would remove overlay current pointer {current}")
            else:
                print(f"overlay current pointer is already absent: {current}")
            if not actions:
                print(f"no overlay-managed symlinks found for {owner}")
            return

        initial_state_snapshot = _bind_managed_state_parent_for_pending_staging(
            home,
            initial_state_snapshot,
            loaded_state,
        )
        _ensure_current_can_switch(home, owner)
        current_snapshot = _capture_reconcile_target_snapshot(home, current)
        current_actions: list[ReconcileAction] = []
        if current_snapshot.link_identity is not None:
            previous_current = current_snapshot.link_target
            expected_current = f"releases/{outgoing_sha}"
            if previous_current != expected_current:
                raise SyncError(
                    f"current release mismatch for owner {owner}: "
                    f"expected {expected_current}, got {previous_current}"
                )
            current_actions.append(
                ReconcileAction(
                    "remove",
                    current,
                    "",
                    "directory",
                    expected_link_target=previous_current,
                    planned_snapshot=current_snapshot,
                )
            )
        outgoing_release = active_expectations.get(owner)
        if outgoing_release is None:
            outgoing_expectation = (
                _installed_release_identity_and_directory_identity(
                    home,
                    owner,
                    outgoing_sha,
                )
            )
            if outgoing_expectation[0][1] != outgoing_manifest:
                raise SyncError(
                    f"managed link state release manifest mismatch for owner {owner}"
                )
            outgoing_release = ActiveReleaseExpectation(
                owner=owner,
                sha=outgoing_sha,
                manifest=outgoing_manifest,
                expectation=outgoing_expectation,
            )
            active_expectations[owner] = outgoing_release
        elif (
            outgoing_release.sha != outgoing_sha
            or outgoing_release.manifest != outgoing_manifest
        ):
            raise SyncError(f"outgoing release identity mismatch for owner {owner}")
        missing_remaining = set(next_manifests).difference(active_expectations)
        if missing_remaining:
            raise SyncError(
                "remaining active release identity is missing for owner(s): "
                + ", ".join(sorted(missing_remaining))
            )
        active_bindings = _open_active_release_bindings(
            home,
            active_expectations,
        )
        remaining_bindings = {
            current_owner: active_bindings[current_owner]
            for current_owner in next_manifests
        }
        outgoing_binding = active_bindings[owner]
        held_bindings = list(active_bindings.values())
        pending_batch: PendingLinkBatch | None = None
        link_transaction: ReconcileTransaction | None = None
        current_transaction: ReconcileTransaction | None = None
        state_transaction: ManagedStateFileTransaction | None = None
        state_committed = False
        try:
            expected_owner_shas = {
                current_owner: active_expectations[current_owner].sha
                for current_owner in next_manifests
            }
            planned_next_state = _planned_committed_state(
                home,
                desired_entries,
                expected_owner_shas,
                managed_targets,
            )
            pending_batch = _stage_pending_link_batch(
                home,
                [("managed", actions), ("current", current_actions)],
                desired_entries,
                expected_owner_shas,
                initial_state_snapshot,
                state,
                planned_next_state,
                required_replacements_by_scope={
                    "managed": required_replacements,
                },
            )
            _publish_pending_link_pointer(home, pending_batch)
            _verify_install_release_identities(
                home,
                list(remaining_bindings.values()),
                phase="before overlay uninstall",
                verify_current=True,
            )
            _verify_install_release_identities(
                home,
                [outgoing_binding],
                phase="before overlay uninstall",
                verify_current=bool(current_actions),
            )
            if not current_actions and (
                _capture_reconcile_target_snapshot(home, current) != current_snapshot
            ):
                raise SyncError(
                    f"missing current pointer changed before overlay uninstall: {current}"
                )
            link_transaction = ReconcileTransaction(
                batch_root=pending_batch.batch_root,
                mutations=[],
            )
            _apply_reconcile_actions(
                home,
                actions,
                dry_run=False,
                required_replacements=required_replacements,
                pending_batch=pending_batch,
                pending_scope="managed",
                batch_root=pending_batch.batch_root,
                transaction=link_transaction,
            )
            current_transaction = ReconcileTransaction(
                batch_root=pending_batch.batch_root,
                mutations=[],
            )
            _apply_reconcile_actions(
                home,
                current_actions,
                dry_run=False,
                pending_batch=pending_batch,
                pending_scope="current",
                batch_root=pending_batch.batch_root,
                transaction=current_transaction,
            )
            if current_actions:
                print(f"removed overlay current pointer {current}")
            _verify_install_release_identities(
                home,
                list(remaining_bindings.values()),
                phase="during overlay uninstall",
                verify_current=True,
            )
            _verify_install_release_identities(
                home,
                [outgoing_binding],
                phase="during overlay uninstall",
                verify_current=False,
            )
            if not current_actions and (
                _capture_reconcile_target_snapshot(home, current) != current_snapshot
            ):
                raise SyncError(
                    f"missing current pointer changed during overlay uninstall: {current}"
                )
            _verify_desired_entries(home, desired_entries)
            for manifest in overlay_manifests:
                issues = _collect_overlay_issues(home, manifest.owner)
                if issues:
                    for issue in issues:
                        print(f"overlay issue: {issue}")
                    raise SyncError(
                        f"overlay verification failed with {len(issues)} issue(s)"
                    )

            owner_shas = _owner_shas_from_bound_current_releases(
                home,
                next_manifests,
                expected_owner_shas,
                remaining_bindings,
                phase="before overlay uninstall state publication",
            )
            next_state = _committed_state(
                home,
                desired_entries,
                owner_shas,
                managed_targets,
            )
            if next_state != planned_next_state:
                raise SyncError("observed uninstall state differs from the pending plan")
            managed_link_snapshots = _trusted_managed_link_snapshots_for_state(
                home,
                next_state,
                baseline_link_snapshots,
                link_transaction,
            )
            _verify_install_release_bindings_lightweight(
                home,
                list(remaining_bindings.values()),
                phase="during final overlay uninstall state validation",
                verify_current=True,
            )
            _verify_install_release_bindings_lightweight(
                home,
                [outgoing_binding],
                phase="during final overlay uninstall state validation",
                verify_current=False,
            )
            _verify_desired_entries(home, desired_entries)
            _verify_managed_link_snapshots(
                home,
                next_state,
                managed_link_snapshots,
            )
            state_transaction = _prepare_pending_managed_state_transaction(
                home,
                pending_batch,
                next_state,
            )
            _verify_install_release_identities(
                home,
                list(remaining_bindings.values()),
                phase="during final overlay uninstall state validation",
                verify_current=True,
            )
            _verify_install_release_identities(
                home,
                [outgoing_binding],
                phase="during final overlay uninstall state validation",
                verify_current=False,
            )
            _verify_desired_entries(home, desired_entries)
            _verify_managed_link_snapshots(
                home,
                next_state,
                managed_link_snapshots,
            )
            _write_managed_state(home, next_state, state_transaction)
            _verify_published_state_transaction(home, state_transaction)
            _verify_install_release_identities(
                home,
                list(remaining_bindings.values()),
                phase="after overlay uninstall managed-state publication",
                verify_current=True,
            )
            _verify_install_release_identities(
                home,
                [outgoing_binding],
                phase="after overlay uninstall managed-state publication",
                verify_current=False,
            )
            _verify_desired_entries(home, desired_entries)
            _verify_managed_link_snapshots(
                home,
                next_state,
                managed_link_snapshots,
            )
            _verify_committed_pending_link_records(home, pending_batch)
            _verify_published_state_transaction(home, state_transaction)
            _verify_install_release_identities(
                home,
                list(remaining_bindings.values()),
                phase="before overlay uninstall commit",
                verify_current=True,
            )
            _verify_install_release_identities(
                home,
                [outgoing_binding],
                phase="before overlay uninstall commit",
                verify_current=False,
            )
            if not current_actions and (
                _capture_reconcile_target_snapshot(home, current) != current_snapshot
            ):
                raise SyncError(
                    f"missing current pointer changed before overlay uninstall commit: "
                    f"{current}"
                )
            _publish_pending_commit_marker(home, pending_batch)
            state_committed = True
            _mark_pending_batch_cleanup_ready(home, pending_batch)
            _clear_pending_link_pointer(home, pending_batch, phase="after")
        except BaseException as error:
            if (
                not state_committed
                and pending_batch is not None
                and pending_batch.pointer_snapshot is not None
            ):
                try:
                    state_committed = _pending_commit_decision(home, pending_batch)
                except (OSError, SyncError) as marker_error:
                    _close_install_release_bindings(held_bindings)
                    raise SyncError(
                        "overlay uninstall failed with an ambiguous commit marker; "
                        "the pending transaction was retained: "
                        f"{marker_error}"
                    ) from error
            if state_committed:
                _close_install_release_bindings(held_bindings)
                raise SyncError(
                    "overlay uninstall committed managed state but finalization "
                    "failed; the pending transaction was retained for exact recovery"
                ) from error
            rollback_errors: list[str] = []
            try:
                _restore_managed_state_file(home, state_transaction)
            except (OSError, SyncError) as rollback_error:
                rollback_errors.append(f"state: {rollback_error}")
            try:
                _rollback_reconcile_transaction(home, current_transaction)
            except (OSError, SyncError) as rollback_error:
                rollback_errors.append(f"current: {rollback_error}")
            try:
                _rollback_reconcile_transaction(home, link_transaction)
            except (OSError, SyncError) as rollback_error:
                rollback_errors.append(f"links: {rollback_error}")
            try:
                _verify_install_release_identities(
                    home,
                    list(remaining_bindings.values()),
                    phase="during overlay uninstall rollback",
                    verify_current=True,
                )
                _verify_install_release_identities(
                    home,
                    [outgoing_binding],
                    phase="during overlay uninstall rollback",
                    verify_current=bool(current_actions),
                )
                if not current_actions and (
                    _capture_reconcile_target_snapshot(home, current)
                    != current_snapshot
                ):
                    raise SyncError(
                        "missing current pointer changed during overlay uninstall "
                        f"rollback: {current}"
                    )
            except (OSError, SyncError) as rollback_error:
                rollback_errors.append(f"releases: {rollback_error}")
            if (
                pending_batch is not None
                and pending_batch.pointer_snapshot is not None
                and not rollback_errors
            ):
                try:
                    _clear_pending_link_pointer(home, pending_batch, phase="before")
                except (OSError, SyncError) as rollback_error:
                    rollback_errors.append(f"pending pointer: {rollback_error}")
            if rollback_errors:
                _close_install_release_bindings(held_bindings)
                raise SyncError(
                    f"overlay uninstall failed: {error}; rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from error
            _close_install_release_bindings(held_bindings)
            raise
        try:
            _commit_managed_state_transaction(state_transaction)
            _commit_reconcile_transaction(current_transaction)
            _commit_reconcile_transaction(link_transaction)
            assert pending_batch is not None
            _try_cleanup_committed_pending_batch(home, pending_batch)
        finally:
            _close_install_release_bindings(held_bindings)
        if not actions:
            print(f"no overlay-managed symlinks found for {owner}")

    if dry_run:
        apply_uninstall()
        return

    with installation_lock(home):
        apply_uninstall()


def _codex_user_home(home: Path) -> Path:
    home = home.expanduser()
    user_home = Path.home().expanduser()
    expected = user_home / ".codex"
    if home != expected:
        raise SyncError(
            f"scheduler --home must point at current user's ~/.codex: {home} "
            f"(expected {expected})"
        )
    return user_home


def _scheduler_runner(home: Path, runner: str | None) -> Path:
    if runner:
        return Path(runner).expanduser()
    return home.expanduser() / "bin" / "codex-personal-sync"


def _validate_scheduler_runner(runner: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if not runner.exists():
        raise SyncError(
            f"scheduler runner is missing: {runner}; run install first or pass --runner"
        )
    if not os.access(runner, os.X_OK):
        raise SyncError(f"scheduler runner is not executable: {runner}")


def _scheduler_platform(raw_platform: str) -> str:
    if raw_platform != "auto":
        return raw_platform
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise SyncError(f"unsupported scheduler platform: {sys.platform}")


def _scheduler_paths(platform_name: str, home: Path) -> SchedulerPaths:
    user_home = _codex_user_home(home)
    if platform_name == "macos":
        return SchedulerPaths(
            platform="macos",
            launchd_plist=user_home
            / "Library"
            / "LaunchAgents"
            / f"{LAUNCHD_LABEL}.plist",
        )
    if platform_name == "linux":
        systemd_root = user_home / ".config" / "systemd" / "user"
        return SchedulerPaths(
            platform="linux",
            systemd_service=systemd_root / f"{SYSTEMD_UNIT}.service",
            systemd_timer=systemd_root / f"{SYSTEMD_UNIT}.timer",
        )
    raise SyncError(f"unsupported scheduler platform: {platform_name}")


def _legacy_launchd_plist(paths: SchedulerPaths, label: str) -> Path:
    assert paths.launchd_plist is not None
    return paths.launchd_plist.parent / f"{label}.plist"


def _cleanup_legacy_launchd_schedulers(
    paths: SchedulerPaths,
    *,
    dry_run: bool,
    disable: bool,
    remove: bool,
) -> None:
    if paths.launchd_plist is None:
        return
    if not remove:
        return
    domain = f"gui/{os.getuid()}"
    for label in LEGACY_LAUNCHD_LABELS:
        legacy_plist = _legacy_launchd_plist(paths, label)
        if disable:
            _run_native_command(
                ["launchctl", "bootout", domain, str(legacy_plist)],
                dry_run=dry_run,
                allow_fail=True,
            )
            _run_native_command(
                ["launchctl", "disable", f"{domain}/{label}"],
                dry_run=dry_run,
                allow_fail=True,
            )
        _unlink_file(legacy_plist, dry_run=dry_run)


def _scheduler_log_dir(home: Path) -> Path:
    return _personal_sync_root(home.expanduser()) / "logs"


def _scheduler_install_args(
    runner: Path,
    repo: str,
    home: Path,
    *,
    mode: str = "public",
    base_repo: str = DEFAULT_PUBLIC_RELEASE_REPO,
    owner: str = "private",
) -> list[str]:
    if mode == "public":
        return [str(runner), "install", "--repo", repo, "--home", str(home.expanduser())]
    if mode == "private":
        return [
            str(runner),
            "install-private",
            "--repo",
            repo,
            "--base-repo",
            base_repo,
            "--owner",
            owner,
            "--home",
            str(home.expanduser()),
        ]
    raise SyncError(f"unsupported scheduler mode: {mode}")


def _launchd_plist(
    home: Path,
    repo: str,
    interval_minutes: int,
    runner: Path,
    *,
    mode: str = "public",
    base_repo: str = DEFAULT_PUBLIC_RELEASE_REPO,
    owner: str = "private",
) -> dict[str, Any]:
    log_dir = _scheduler_log_dir(home)
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": _scheduler_install_args(
            runner,
            repo,
            home,
            mode=mode,
            base_repo=base_repo,
            owner=owner,
        ),
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "codex-personal-sync.out.log"),
        "StandardErrorPath": str(log_dir / "codex-personal-sync.err.log"),
        "EnvironmentVariables": {"PATH": MACOS_SCHEDULER_PATH},
    }


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_service(
    home: Path,
    repo: str,
    runner: Path,
    *,
    mode: str = "public",
    base_repo: str = DEFAULT_PUBLIC_RELEASE_REPO,
    owner: str = "private",
) -> str:
    exec_start = " ".join(
        _systemd_quote(arg)
        for arg in _scheduler_install_args(
            runner,
            repo,
            home,
            mode=mode,
            base_repo=base_repo,
            owner=owner,
        )
    )
    return "\n".join(
        [
            "[Unit]",
            "Description=Personal Codex config sync",
            "",
            "[Service]",
            "Type=oneshot",
            f"Environment={_systemd_quote(f'PATH={LINUX_SCHEDULER_PATH}')}",
            f"ExecStart={exec_start}",
            "",
        ]
    )


def _systemd_timer(interval_minutes: int) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Run Personal Codex config sync periodically",
            "",
            "[Timer]",
            "OnBootSec=5min",
            f"OnUnitActiveSec={interval_minutes}min",
            "Persistent=true",
            f"Unit={SYSTEMD_UNIT}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def _run_native_command(args: list[str], *, dry_run: bool, allow_fail: bool = False) -> None:
    if dry_run:
        print("would run: " + " ".join(args))
        return
    try:
        completed = subprocess.run(
            args,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as error:
        if allow_fail:
            print(f"ignored failed command {' '.join(args)}: {error}")
            return
        raise SyncError(f"failed to run {' '.join(args)}: {error}") from error
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        if allow_fail:
            print(f"ignored failed command {' '.join(args)}: {message}")
            return
        raise SyncError(message or f"command failed: {' '.join(args)}")


def _write_text(path: Path, content: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"would write {path}")
        print(content.rstrip())
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_plist(path: Path, payload: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        print(f"would write {path}")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        plistlib.dump(payload, file, sort_keys=True)


def install_scheduler(
    home: Path,
    repo: str,
    interval_minutes: int,
    platform_name: str,
    runner: str | None,
    *,
    dry_run: bool,
    enable: bool,
    mode: str = "public",
    base_repo: str = DEFAULT_PUBLIC_RELEASE_REPO,
    owner: str = "private",
) -> None:
    if interval_minutes < 1:
        raise SyncError("scheduler interval must be at least 1 minute")
    if mode not in {"public", "private"}:
        raise SyncError(f"unsupported scheduler mode: {mode}")
    owner = _validate_owner(owner)
    if mode == "private" and owner == PUBLIC_OWNER:
        raise SyncError("private scheduler owner must not be public")
    home = home.expanduser()
    selected_platform = _scheduler_platform(platform_name)
    runner_path = _scheduler_runner(home, runner)
    _validate_scheduler_runner(runner_path, dry_run=dry_run)
    paths = _scheduler_paths(selected_platform, home)
    if selected_platform == "macos":
        assert paths.launchd_plist is not None
        if not dry_run:
            _ensure_safe_internal_directory(
                home,
                _scheduler_log_dir(home),
                create=True,
            )
        _write_plist(
            paths.launchd_plist,
            _launchd_plist(
                home,
                repo,
                interval_minutes,
                runner_path,
                mode=mode,
                base_repo=base_repo,
                owner=owner,
            ),
            dry_run=dry_run,
        )
        _cleanup_legacy_launchd_schedulers(
            paths,
            dry_run=dry_run,
            disable=enable,
            remove=enable,
        )
        if enable:
            domain = f"gui/{os.getuid()}"
            _run_native_command(
                ["launchctl", "bootout", domain, str(paths.launchd_plist)],
                dry_run=dry_run,
                allow_fail=True,
            )
            _run_native_command(
                ["launchctl", "bootstrap", domain, str(paths.launchd_plist)],
                dry_run=dry_run,
            )
            _run_native_command(
                ["launchctl", "enable", f"{domain}/{LAUNCHD_LABEL}"],
                dry_run=dry_run,
            )
        print(f"installed macOS launchd scheduler: {paths.launchd_plist}")
        return

    if selected_platform == "linux":
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        _write_text(
            paths.systemd_service,
            _systemd_service(
                home,
                repo,
                runner_path,
                mode=mode,
                base_repo=base_repo,
                owner=owner,
            ),
            dry_run=dry_run,
        )
        _write_text(paths.systemd_timer, _systemd_timer(interval_minutes), dry_run=dry_run)
        if enable:
            _run_native_command(["systemctl", "--user", "daemon-reload"], dry_run=dry_run)
            _run_native_command(
                ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"],
                dry_run=dry_run,
            )
        print(f"installed Linux systemd user scheduler: {paths.systemd_timer}")
        return

    raise SyncError(f"unsupported scheduler platform: {selected_platform}")


def _unlink_file(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        print(f"would remove {path}")
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def uninstall_scheduler(
    home: Path,
    platform_name: str,
    *,
    dry_run: bool,
    disable: bool,
) -> None:
    home = home.expanduser()
    selected_platform = _scheduler_platform(platform_name)
    paths = _scheduler_paths(selected_platform, home)
    if selected_platform == "macos":
        assert paths.launchd_plist is not None
        if disable:
            domain = f"gui/{os.getuid()}"
            _run_native_command(
                ["launchctl", "bootout", domain, str(paths.launchd_plist)],
                dry_run=dry_run,
                allow_fail=True,
            )
            _run_native_command(
                ["launchctl", "disable", f"{domain}/{LAUNCHD_LABEL}"],
                dry_run=dry_run,
                allow_fail=True,
            )
        _cleanup_legacy_launchd_schedulers(
            paths,
            dry_run=dry_run,
            disable=disable,
            remove=True,
        )
        _unlink_file(paths.launchd_plist, dry_run=dry_run)
        print(f"removed macOS launchd scheduler: {paths.launchd_plist}")
        return

    if selected_platform == "linux":
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        if disable:
            _run_native_command(
                ["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"],
                dry_run=dry_run,
                allow_fail=True,
            )
        _unlink_file(paths.systemd_timer, dry_run=dry_run)
        _unlink_file(paths.systemd_service, dry_run=dry_run)
        if disable:
            _run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=dry_run,
                allow_fail=True,
            )
        print(f"removed Linux systemd user scheduler: {paths.systemd_timer}")
        return

    raise SyncError(f"unsupported scheduler platform: {selected_platform}")


def _non_empty_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def default_release_repo() -> str | None:
    return _non_empty_env(DEFAULT_RELEASE_REPO_ENV)


def default_base_release_repo() -> str:
    return _non_empty_env(DEFAULT_BASE_RELEASE_REPO_ENV) or DEFAULT_PUBLIC_RELEASE_REPO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install personal Codex config from GitHub release assets."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    release_repo = default_release_repo()
    base_release_repo = default_base_release_repo()

    install_parser = subparsers.add_parser("install", help="Download and install latest release")
    install_parser.add_argument("--repo", default=release_repo, required=release_repo is None)
    install_parser.add_argument("--home", default="~/.codex")
    install_parser.add_argument("--dry-run", action="store_true")

    install_private_parser = subparsers.add_parser(
        "install-private",
        help="Install a public base release and then a private overlay release",
    )
    install_private_parser.add_argument(
        "--repo",
        default=release_repo,
        required=release_repo is None,
        help="Private overlay release repository",
    )
    install_private_parser.add_argument("--base-repo", default=base_release_repo)
    install_private_parser.add_argument("--owner", default="private")
    install_private_parser.add_argument("--home", default="~/.codex")
    install_private_parser.add_argument("--dry-run", action="store_true")

    status_parser = subparsers.add_parser("status", help="Show current release and link state")
    status_parser.add_argument("--home", default="~/.codex")
    status_parser.add_argument("--owner", default=PUBLIC_OWNER)

    rollback_parser = subparsers.add_parser("rollback", help="Switch current to an older release")
    rollback_parser.add_argument("--home", default="~/.codex")
    rollback_parser.add_argument("--owner", default=PUBLIC_OWNER)
    rollback_parser.add_argument("--to", help="Exact or unique release SHA prefix")

    verify_overlay_parser = subparsers.add_parser(
        "verify-overlay",
        help="Verify an installed private overlay against the public base",
    )
    verify_overlay_parser.add_argument("--home", default="~/.codex")
    verify_overlay_parser.add_argument("--owner", default="private")

    uninstall_overlay_parser = subparsers.add_parser(
        "uninstall-overlay",
        help="Remove an overlay and restore public links for declared overrides",
    )
    uninstall_overlay_parser.add_argument("--home", default="~/.codex")
    uninstall_overlay_parser.add_argument("--owner", default="private")
    uninstall_overlay_parser.add_argument("--dry-run", action="store_true")

    scheduler_parser = subparsers.add_parser(
        "install-scheduler",
        help="Install a user-level scheduler that periodically runs install",
    )
    scheduler_parser.add_argument("--repo", default=release_repo, required=release_repo is None)
    scheduler_parser.add_argument("--mode", choices=("public", "private"), default="public")
    scheduler_parser.add_argument("--base-repo", default=base_release_repo)
    scheduler_parser.add_argument("--owner", default="private")
    scheduler_parser.add_argument("--home", default="~/.codex")
    scheduler_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_SCHEDULER_INTERVAL_MINUTES,
    )
    scheduler_parser.add_argument("--platform", choices=("auto", "macos", "linux"), default="auto")
    scheduler_parser.add_argument("--runner", help="Executable sync script path")
    scheduler_parser.add_argument("--dry-run", action="store_true")
    scheduler_parser.add_argument(
        "--no-enable",
        action="store_true",
        help="Write scheduler files without loading/enabling them",
    )

    unscheduler_parser = subparsers.add_parser(
        "uninstall-scheduler",
        help="Disable and remove the user-level scheduler",
    )
    unscheduler_parser.add_argument("--home", default="~/.codex")
    unscheduler_parser.add_argument("--platform", choices=("auto", "macos", "linux"), default="auto")
    unscheduler_parser.add_argument("--dry-run", action="store_true")
    unscheduler_parser.add_argument(
        "--no-disable",
        action="store_true",
        help="Remove scheduler files without calling launchctl/systemctl",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            install_from_github(args.repo, Path(args.home), dry_run=args.dry_run)
        elif args.command == "install-private":
            install_private_from_github(
                args.repo,
                Path(args.home),
                base_repo=args.base_repo,
                owner=args.owner,
                dry_run=args.dry_run,
            )
        elif args.command == "status":
            status(Path(args.home), args.owner)
        elif args.command == "rollback":
            rollback(Path(args.home), args.to, args.owner)
        elif args.command == "verify-overlay":
            verify_overlay(Path(args.home), args.owner)
        elif args.command == "uninstall-overlay":
            uninstall_overlay(Path(args.home), args.owner, dry_run=args.dry_run)
        elif args.command == "install-scheduler":
            install_scheduler(
                Path(args.home),
                args.repo,
                args.interval_minutes,
                args.platform,
                args.runner,
                dry_run=args.dry_run,
                enable=not args.no_enable,
                mode=args.mode,
                base_repo=args.base_repo,
                owner=args.owner,
            )
        elif args.command == "uninstall-scheduler":
            uninstall_scheduler(
                Path(args.home),
                args.platform,
                dry_run=args.dry_run,
                disable=not args.no_disable,
            )
        else:
            parser.error(f"unknown command: {args.command}")
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
