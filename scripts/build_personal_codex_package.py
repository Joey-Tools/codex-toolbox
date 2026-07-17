#!/usr/bin/env python3
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
import gzip
import hashlib
import io
from itertools import chain
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import selectors
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any
import unicodedata


DEFAULT_MANIFEST = Path("personal_codex/public-sync-manifest.json")
RELEASE_MANIFEST = Path("personal_codex/sync-manifest.json")
GENERATED_DIR_NAMES = frozenset({"__pycache__"})
GENERATED_FILE_NAMES = frozenset({".DS_Store"})
GENERATED_SUFFIXES = frozenset({".pyc", ".pyo"})
GIT_INVENTORY_LIMIT_BYTES = 16 * 1024 * 1024
GIT_ERROR_TAIL_BYTES = 64 * 1024
GIT_OBJECT_SIZE_OUTPUT_LIMIT_BYTES = 64
GIT_PATHSPEC_ARG_BUDGET_BYTES = 32 * 1024
MAX_RELEASE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 4300
MAX_ARCHIVE_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBER_PATH_BYTES = 4096
MAX_ARCHIVE_MEMBER_COMPONENT_BYTES = 255
MAX_ARCHIVE_MEMBER_PATH_DEPTH = 64
MAX_MANIFEST_TARGET_PATH_BYTES = 4096
MAX_MANIFEST_TARGET_COMPONENT_BYTES = 255
MAX_MANIFEST_TARGET_PATH_DEPTH = 64
MAX_OWNER_COMPONENT_BYTES = 255
MAX_MANAGED_LINK_TARGET_BYTES = 1023
MAX_PENDING_LINK_RECORDS = 10_000
MAX_PENDING_LINK_CLAIMS = 20_000
# A first-install transaction also records and claims the owner's current link.
MAX_MANIFEST_ACTIVE_LINKS = (
    min(MAX_PENDING_LINK_RECORDS, MAX_PENDING_LINK_CLAIMS) - 1
)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
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
BASE_RELEASE_FIELDS = frozenset({"repo", "sha"})
REGULAR_GIT_MODES = frozenset({b"100644", b"100755"})
PUBLIC_OWNER = "public"
RESERVED_TARGET_ROOTS = (
    PurePosixPath("personal-sync"),
    PurePosixPath(".personal-sync-pending-transaction.json"),
)


class PackageError(RuntimeError):
    pass


def _bounded_json_integer(raw_value: str) -> int:
    digits = raw_value[1:] if raw_value.startswith("-") else raw_value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits"
        )
    return int(raw_value)


@dataclass(frozen=True)
class _IndexEntry:
    mode: bytes
    object_id: bytes
    stage: bytes


@dataclass(frozen=True)
class _TreeEntry:
    mode: bytes
    object_type: bytes
    object_id: bytes


@dataclass(frozen=True)
class _SnapshotFile:
    path: Path
    mode: bytes
    object_id: bytes
    size: int | None = None


@dataclass
class _StrictPathIndexNode:
    children: dict[str, _StrictPathIndexNode] = field(default_factory=dict)
    terminal: Path | None = None


@dataclass(frozen=True)
class StrictReleaseSnapshot:
    manifest: dict[str, Any]
    manifest_mode: bytes
    directories: tuple[Path, ...]
    files: tuple[_SnapshotFile, ...]
    manifest_payload: bytes | None = None


def _canonical_repo_root(repo_root: Path) -> Path:
    try:
        root = repo_root.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise PackageError(f"failed to resolve repository root {repo_root}: {error}") from error
    if not root.is_dir():
        raise PackageError(f"repository root is not a directory: {repo_root}")
    return root


def _validated_repo_path(repo_root: Path, relative_path: Path, label: str) -> tuple[Path, int]:
    raw_path = relative_path.as_posix()
    if "\0" in raw_path:
        raise PackageError(f"refusing {label} with embedded NUL")
    try:
        raw_path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise PackageError(f"refusing {label} that is not valid UTF-8") from error
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
        or any(part in {"", "."} for part in relative_path.parts)
    ):
        raise PackageError(f"refusing unsafe {label}: {relative_path}")

    root = _canonical_repo_root(repo_root)
    current = root
    for index, part in enumerate(relative_path.parts):
        current /= part
        try:
            mode = current.lstat().st_mode
        except (OSError, ValueError) as error:
            raise PackageError(f"{label} is missing or unreadable: {relative_path}: {error}") from error
        if stat.S_ISLNK(mode):
            prefix = Path(*relative_path.parts[: index + 1])
            raise PackageError(f"refusing {label} with symlink path component: {prefix}")
        if index < len(relative_path.parts) - 1 and not stat.S_ISDIR(mode):
            prefix = Path(*relative_path.parts[: index + 1])
            raise PackageError(f"{label} ancestor is not a directory: {prefix}")

    try:
        current.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise PackageError(f"refusing {label} outside repository root: {relative_path}") from error
    return current, mode


def _reject_nested_git_marker_ancestors(repo_root: Path, relative_path: Path, label: str) -> None:
    root = _canonical_repo_root(repo_root)
    current = root
    for index, part in enumerate(relative_path.parts):
        current /= part
        mode = current.lstat().st_mode
        if not stat.S_ISDIR(mode):
            continue
        marker = current / ".git"
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise PackageError(f"failed to inspect nested Git marker {marker}: {error}") from error
        prefix = Path(*relative_path.parts[: index + 1])
        raise PackageError(f"refusing nested Git repository in {label} path: {prefix}")


def _parse_manifest_bytes(payload: bytes, manifest_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            payload.decode("utf-8"),
            parse_int=_bounded_json_integer,
        )
    except UnicodeDecodeError as error:
        raise PackageError(f"manifest {manifest_path} is not valid UTF-8: {error}") from error
    except (ValueError, RecursionError) as error:
        raise PackageError(f"manifest {manifest_path} is invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise PackageError(f"manifest {manifest_path} must be a JSON object")
    return data


def _validate_manifest_unicode_scalars(manifest: object) -> None:
    pending = [iter((manifest,))]
    seen_containers: set[int] = set()
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                raise PackageError(
                    "manifest contains a string that is not valid UTF-8"
                )
            continue
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.append(chain(value.keys(), value.values()))
            continue
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.append(iter(value))


def _json_string_token_exceeds_limit(value: str, limit: int) -> bool:
    encoded_size = 2
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"}:
            encoded_size += 2
        elif codepoint <= 0x1F:
            encoded_size += 2 if character in "\b\f\n\r\t" else 6
        elif codepoint <= 0x7F:
            encoded_size += 1
        elif codepoint <= 0xFFFF:
            encoded_size += 6
        else:
            encoded_size += 12
        if encoded_size > limit:
            return True
    return encoded_size > limit


def _validate_manifest_json_string_tokens(manifest: object) -> None:
    pending = [iter((manifest,))]
    seen_containers: set[int] = set()
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, str):
            if _json_string_token_exceeds_limit(
                value,
                MAX_RELEASE_MANIFEST_BYTES,
            ):
                raise PackageError(
                    "serialized release manifest exceeds "
                    f"{MAX_RELEASE_MANIFEST_BYTES} bytes"
                )
            continue
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.append(chain(value.keys(), value.values()))
            continue
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.append(iter(value))


def _release_manifest_payload(manifest: dict[str, Any]) -> bytes:
    _validate_manifest_unicode_scalars(manifest)
    _validate_manifest_json_string_tokens(manifest)
    output = io.BytesIO()
    try:
        encoder = json.JSONEncoder(indent=2, sort_keys=False, ensure_ascii=True)
        for chunk in encoder.iterencode(manifest):
            remaining = MAX_RELEASE_MANIFEST_BYTES - output.tell()
            if len(chunk) > remaining:
                raise PackageError(
                    "serialized release manifest exceeds "
                    f"{MAX_RELEASE_MANIFEST_BYTES} bytes"
                )
            output.write(chunk.encode("ascii", errors="strict"))
        if output.tell() >= MAX_RELEASE_MANIFEST_BYTES:
            raise PackageError(
                "serialized release manifest exceeds "
                f"{MAX_RELEASE_MANIFEST_BYTES} bytes"
            )
        output.write(b"\n")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise PackageError(f"failed to serialize release manifest: {error}") from error
    return output.getvalue()


def _load_manifest(repo_root: Path, manifest_path: Path) -> dict[str, Any]:
    absolute_path, mode = _validated_repo_path(repo_root, manifest_path, "manifest")
    if not stat.S_ISREG(mode):
        raise PackageError(f"manifest is not a regular file: {manifest_path}")
    _reject_nested_git_marker_ancestors(repo_root, manifest_path, "manifest")
    try:
        with absolute_path.open("rb") as manifest_file:
            payload = manifest_file.read(MAX_RELEASE_MANIFEST_BYTES + 1)
    except (OSError, ValueError) as error:
        raise PackageError(f"failed to read manifest {manifest_path}: {error}") from error
    if len(payload) > MAX_RELEASE_MANIFEST_BYTES:
        raise PackageError(
            f"manifest {manifest_path} exceeds {MAX_RELEASE_MANIFEST_BYTES} bytes"
        )
    return _parse_manifest_bytes(payload, manifest_path)


def _validate_manifest_path_encoding(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise PackageError(f"{field} must be a non-empty string path")
    if "\0" in raw:
        raise PackageError(f"{field} must not contain embedded NUL")
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise PackageError(f"{field} must be valid UTF-8") from error
    return raw


def _validate_manifest_relative_path(raw: object, field: str) -> PurePosixPath:
    value = _validate_manifest_path_encoding(raw, field)
    raw_parts = value.split("/")
    if (
        value.startswith("/")
        or ".." in raw_parts
        or any(part in {"", "."} for part in raw_parts)
    ):
        raise PackageError(f"refusing unsafe {field}: {value}")
    path = PurePosixPath(value)
    return path


def _portable_manifest_path_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in path.parts
    )


def _validate_manifest_target_path(raw: object, field: str) -> PurePosixPath:
    path = _validate_manifest_relative_path(raw, field)
    encoded = path.as_posix().encode("utf-8")
    if len(encoded) > MAX_MANIFEST_TARGET_PATH_BYTES:
        raise PackageError(
            f"{field} exceeds {MAX_MANIFEST_TARGET_PATH_BYTES} UTF-8 bytes"
        )
    if len(path.parts) > MAX_MANIFEST_TARGET_PATH_DEPTH:
        raise PackageError(
            f"{field} exceeds {MAX_MANIFEST_TARGET_PATH_DEPTH} path components"
        )
    for index, part in enumerate(path.parts, start=1):
        component_bytes = len(part.encode("utf-8"))
        if component_bytes > MAX_MANIFEST_TARGET_COMPONENT_BYTES:
            raise PackageError(
                f"{field} component {index} exceeds "
                f"{MAX_MANIFEST_TARGET_COMPONENT_BYTES} UTF-8 bytes"
            )
    path_key = _portable_manifest_path_key(path)
    for reserved in RESERVED_TARGET_ROOTS:
        reserved_key = _portable_manifest_path_key(reserved)
        if path_key[: len(reserved_key)] == reserved_key:
            raise PackageError(
                f"{field} must not use reserved personal sync path: {path}"
            )
    return path


def _validate_manifest_owner(raw: object, field: str = "owner") -> str:
    if (
        not isinstance(raw, str)
        or OWNER_RE.fullmatch(raw) is None
        or len(raw.encode("utf-8")) > MAX_OWNER_COMPONENT_BYTES
    ):
        raise PackageError(
            f"{field} must be a non-empty owner id containing only letters, "
            "numbers, '.', '_', or '-', and must not exceed "
            f"{MAX_OWNER_COMPONENT_BYTES} UTF-8 bytes"
        )
    return raw


def _relative_managed_link_target(
    source: PurePosixPath,
    target: PurePosixPath,
    owner: str,
) -> str:
    current = PurePosixPath("personal-sync")
    if owner != PUBLIC_OWNER:
        current = current / "overlays" / owner
    current = current / "current"
    return posixpath.relpath(
        (current / source).as_posix(),
        start=target.parent.as_posix(),
    )


def _validate_active_managed_link_target(
    source: PurePosixPath,
    target: PurePosixPath,
    owner: str,
    field: str,
) -> None:
    link_target = _relative_managed_link_target(source, target, owner)
    if len(link_target.encode("utf-8")) > MAX_MANAGED_LINK_TARGET_BYTES:
        raise PackageError(
            f"{field} managed symlink target exceeds "
            f"{MAX_MANAGED_LINK_TARGET_BYTES} UTF-8 bytes"
        )


def _validate_removed_link_key(raw: object, field: str) -> str:
    if not isinstance(raw, str):
        raise PackageError(f"{field} must be an owner:id string")
    owner, separator, removed_id = raw.partition(":")
    if (
        not separator
        or ":" in removed_id
        or REMOVED_LINK_ID_RE.fullmatch(removed_id) is None
    ):
        raise PackageError(f"{field} must be an owner:id string")
    return f"{_validate_manifest_owner(owner, field)}:{removed_id}"


def _validate_manifest_target_relationships(
    active_targets: list[PurePosixPath],
    historical_targets: list[PurePosixPath],
    all_targets: list[PurePosixPath],
) -> None:
    spellings: dict[tuple[str, ...], PurePosixPath] = {}
    for target in all_targets:
        key = _portable_manifest_path_key(target)
        previous = spellings.get(key)
        if previous is not None and previous != target:
            raise PackageError(
                f"portable target spellings conflict: {previous} and {target}"
            )
        spellings[key] = target

    ordered = sorted(
        {
            (_portable_manifest_path_key(target), target)
            for target in active_targets
        },
        key=lambda item: (item[0], item[1].as_posix()),
    )
    for (parent_key, parent), (child_key, child) in zip(ordered, ordered[1:]):
        if (
            len(parent_key) < len(child_key)
            and child_key[: len(parent_key)] == parent_key
        ):
            raise PackageError(
                f"manifest targets must not overlap: {parent} is an ancestor of {child}"
            )

    historical_by_key = {
        _portable_manifest_path_key(target): target
        for target in historical_targets
    }
    ordered_historical_keys = sorted(historical_by_key)
    for active_target in active_targets:
        active_key = _portable_manifest_path_key(active_target)
        historical_target: PurePosixPath | None = None
        for prefix_length in range(1, len(active_key)):
            historical_target = historical_by_key.get(active_key[:prefix_length])
            if historical_target is not None:
                break
        if historical_target is None:
            descendant_index = bisect_right(ordered_historical_keys, active_key)
            if descendant_index < len(ordered_historical_keys):
                descendant_key = ordered_historical_keys[descendant_index]
                if (
                    len(descendant_key) > len(active_key)
                    and descendant_key[: len(active_key)] == active_key
                ):
                    historical_target = historical_by_key[descendant_key]
        if historical_target is not None:
            raise PackageError(
                "managed target hierarchy changes are not supported: "
                f"{historical_target} -> {active_target}"
            )


def _validate_replacement_retirement_graph(
    graph: dict[str, tuple[str, ...]],
) -> None:
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        cycle_keys = error.args[1] if len(error.args) > 1 else ()
        cycle = " -> ".join(str(key) for key in cycle_keys)
        detail = f": {cycle}" if cycle else ""
        raise PackageError(
            f"replacement retirement cycle detected{detail}"
        ) from None


def _manifest_sources(manifest: dict[str, Any]) -> list[Path]:
    version = manifest.get("version")
    if type(version) is not int or version != 1:
        raise PackageError("sync manifest version must be 1")
    manifest_owner = _validate_manifest_owner(
        manifest["owner"] if "owner" in manifest else PUBLIC_OWNER
    )
    sources: list[Path] = []
    active_targets: list[PurePosixPath] = []
    active_target_set: set[PurePosixPath] = set()
    historical_targets: list[PurePosixPath] = []
    all_targets: list[PurePosixPath] = []
    for section in ("links", "reference_only"):
        items = manifest.get(section, [])
        if not isinstance(items, list):
            raise PackageError(f"manifest {section} must be a list")
        if section == "links" and not items:
            raise PackageError("sync manifest must contain a non-empty links array")
        if section == "links" and len(items) > MAX_MANIFEST_ACTIVE_LINKS:
            raise PackageError(
                "sync manifest active links exceed runtime transaction limit: "
                f"{len(items)} > {MAX_MANIFEST_ACTIVE_LINKS}"
            )
        for item in items:
            if section == "links":
                if not isinstance(item, dict):
                    raise PackageError("manifest link entries must be objects")
                source = item.get("source")
            else:
                source = item
            source_path = _validate_manifest_relative_path(
                source,
                "manifest source",
            )
            if section == "links":
                target = _validate_manifest_target_path(
                    item.get("target"),
                    "manifest link target",
                )
                if target in active_target_set:
                    raise PackageError(f"duplicate manifest target: {target}")
                active_targets.append(target)
                active_target_set.add(target)
                all_targets.append(target)
                kind = item.get("kind")
                if kind not in {"file", "directory", "skill"}:
                    raise PackageError(
                        f"manifest link {source_path} has unsupported kind: {kind}"
                    )
                owner = _validate_manifest_owner(
                    item.get("owner", manifest_owner),
                    "link owner",
                )
                if owner != manifest_owner:
                    raise PackageError(
                        f"manifest link {source_path} owner {owner} does not match "
                        f"manifest owner {manifest_owner}"
                    )
                override = item.get("override", False)
                if not isinstance(override, bool):
                    raise PackageError(
                        f"manifest link {source_path} override must be boolean"
                    )
                if owner == PUBLIC_OWNER and override:
                    raise PackageError(
                        "public manifest links must not declare override=true"
                    )
                _validate_active_managed_link_target(
                    source_path,
                    target,
                    owner,
                    f"manifest link {source_path}",
                )
            sources.append(Path(*source_path.parts))

    removed_links = manifest.get("removed_links", [])
    if not isinstance(removed_links, list):
        raise PackageError("manifest removed_links must be a list")
    removed_ids: set[str] = set()
    removed_records: dict[
        str,
        tuple[PurePosixPath, PurePosixPath | None, tuple[str, ...]],
    ] = {}
    for item in removed_links:
        if not isinstance(item, dict):
            raise PackageError("manifest removed_links entries must be objects")
        unknown_fields = sorted(set(item) - REMOVED_LINK_FIELDS)
        if unknown_fields:
            raise PackageError(
                "removed link has unsupported field(s): "
                + ", ".join(unknown_fields)
            )
        removed_id = item.get("id")
        if (
            not isinstance(removed_id, str)
            or REMOVED_LINK_ID_RE.fullmatch(removed_id) is None
        ):
            raise PackageError("removed link id has unsupported characters")
        if removed_id in removed_ids:
            raise PackageError(f"duplicate removed link id: {removed_id}")
        removed_ids.add(removed_id)
        _validate_manifest_relative_path(
            item.get("source"),
            "removed link source",
        )
        target = _validate_manifest_target_path(
            item.get("target"),
            "removed link target",
        )
        historical_targets.append(target)
        all_targets.append(target)
        replacement_target = item.get("replacement_target")
        replacement: PurePosixPath | None = None
        if replacement_target is not None:
            replacement = _validate_manifest_target_path(
                replacement_target,
                "removed link replacement_target",
            )
            all_targets.append(replacement)
        kind = item.get("kind")
        if kind not in {"file", "directory", "skill"}:
            raise PackageError(
                f"removed link has unsupported kind: {kind}"
            )
        raw_retires = item.get("retires_replacements", [])
        if not isinstance(raw_retires, list):
            raise PackageError(
                f"removed link {removed_id} retires_replacements must be a list"
            )
        retires_replacements = [
            _validate_removed_link_key(
                raw_key,
                f"removed link {removed_id} retires_replacements",
            )
            for raw_key in raw_retires
        ]
        if len(set(retires_replacements)) != len(retires_replacements):
            raise PackageError(
                f"removed link {removed_id} has duplicate "
                "retires_replacements entries"
            )
        legacy = item.get("legacy", False)
        if not isinstance(legacy, bool):
            raise PackageError(f"removed link {removed_id} legacy must be boolean")
        removed_records[removed_id] = (
            target,
            replacement,
            tuple(retires_replacements),
        )

    for removed_id, (target, _replacement, retires) in removed_records.items():
        entry_key = f"{manifest_owner}:{removed_id}"
        for retired_key in retires:
            retired_owner, retired_id = retired_key.split(":", 1)
            if retired_key == entry_key:
                raise PackageError(f"removed link {removed_id} cannot retire itself")
            if retired_owner != manifest_owner:
                continue
            retired = removed_records.get(retired_id)
            if retired is None:
                raise PackageError(
                    f"removed link {removed_id} retires unknown replacement "
                    f"{retired_key}"
                )
            _retired_target, retired_replacement, _retired_retires = retired
            if retired_replacement != target:
                raise PackageError(
                    f"removed link {removed_id} target does not match replacement "
                    f"for {retired_key}"
                )

    removed_keys = {
        f"{manifest_owner}:{removed_id}" for removed_id in removed_records
    }
    retirement_graph = {
        f"{manifest_owner}:{removed_id}": tuple(
            retired_key for retired_key in retires if retired_key in removed_keys
        )
        for removed_id, (_target, _replacement, retires) in removed_records.items()
    }
    _validate_replacement_retirement_graph(retirement_graph)
    if manifest_owner == PUBLIC_OWNER:
        retired_replacement_keys = {
            retired_key
            for _target, _replacement, retires in removed_records.values()
            for retired_key in retires
        }
        for removed_id, (_target, replacement, _retires) in removed_records.items():
            removed_key = f"{manifest_owner}:{removed_id}"
            if (
                replacement is not None
                and replacement not in active_target_set
                and removed_key not in retired_replacement_keys
            ):
                raise PackageError(
                    f"replacement target {replacement} is unavailable for active "
                    f"removal {removed_key}"
                )

    raw_base_release = manifest.get("base_release", {})
    if raw_base_release is None:
        raw_base_release = {}
    if not isinstance(raw_base_release, dict):
        raise PackageError("base_release must be an object when present")
    unknown_fields = sorted(set(raw_base_release) - BASE_RELEASE_FIELDS)
    if unknown_fields:
        raise PackageError(
            "base_release has unsupported field(s): "
            + ", ".join(unknown_fields)
        )
    base_release_repo = raw_base_release.get("repo")
    if base_release_repo is not None and (
        not isinstance(base_release_repo, str)
        or REPOSITORY_RE.fullmatch(base_release_repo) is None
    ):
        raise PackageError("base_release.repo must be an owner/repo string")
    base_release_sha = raw_base_release.get("sha")
    if base_release_sha is not None and (
        not isinstance(base_release_sha, str)
        or FULL_SHA_RE.fullmatch(base_release_sha) is None
    ):
        raise PackageError(
            "base_release.sha must be a 40-character lowercase hex SHA"
        )
    _validate_manifest_target_relationships(
        active_targets,
        historical_targets,
        all_targets,
    )
    return sources


def _validate_manifest_source_kinds(
    manifest: dict[str, Any],
    path_kind: Callable[[Path], str | None],
) -> None:
    raw_links = manifest.get("links")
    assert isinstance(raw_links, list)
    for item in raw_links:
        assert isinstance(item, dict)
        source = Path(*PurePosixPath(item["source"]).parts)
        kind = item["kind"]
        source_type = path_kind(source)
        if kind == "file":
            if source_type != "file":
                raise PackageError(f"manifest file source is missing: {source}")
            continue
        if source_type != "directory":
            raise PackageError(f"manifest directory source is missing: {source}")
        if kind == "skill" and path_kind(source / "SKILL.md") != "file":
            raise PackageError(
                f"manifest skill source is missing SKILL.md: {source}"
            )

    raw_references = manifest.get("reference_only", [])
    assert isinstance(raw_references, list)
    for raw_reference in raw_references:
        reference = Path(*PurePosixPath(raw_reference).parts)
        if path_kind(reference) not in {"file", "directory"}:
            raise PackageError(f"reference_only path is missing: {reference}")


def _live_manifest_path_kind(repo_root: Path, path: Path) -> str | None:
    _absolute_path, mode = _validated_repo_path(
        repo_root,
        path,
        "manifest source",
    )
    _reject_nested_git_marker_ancestors(repo_root, path, "manifest source")
    if _is_generated_path(path, is_dir=stat.S_ISDIR(mode)):
        raise PackageError(f"refusing generated manifest source: {path}")
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return None


def _copy_source(repo_root: Path, staging_root: Path, source: Path) -> None:
    source_path, source_mode = _validated_repo_path(repo_root, source, "manifest source")
    destination = staging_root / source
    if ".git" in source.parts:
        raise PackageError(f"refusing Git repository metadata source: {source}")
    _reject_nested_git_marker_ancestors(repo_root, source, "manifest source")
    if _is_generated_path(source, is_dir=stat.S_ISDIR(source_mode)):
        raise PackageError(f"refusing generated manifest source: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_ISDIR(source_mode):
        destination.mkdir(parents=True, exist_ok=True)
        for child in source_path.rglob("*"):
            relative_child = child.relative_to(source_path)
            if child.name == ".git":
                relative_child = source / relative_child
                raise PackageError(
                    f"refusing nested Git repository source: {relative_child}"
                )
            child_mode = child.lstat().st_mode
            if stat.S_ISLNK(child_mode):
                relative_child = source / relative_child
                raise PackageError(f"refusing to package nested symlink source: {relative_child}")
            if _is_generated_path(relative_child, is_dir=stat.S_ISDIR(child_mode)):
                continue
            destination_child = destination / relative_child
            if stat.S_ISDIR(child_mode):
                destination_child.mkdir(parents=True, exist_ok=True)
            elif stat.S_ISREG(child_mode):
                destination_child.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, destination_child)
            else:
                relative_child = source / relative_child
                raise PackageError(f"unsupported manifest source type: {relative_child}")
    elif stat.S_ISREG(source_mode):
        shutil.copy2(source_path, destination)
    else:
        raise PackageError(f"unsupported manifest source type: {source}")


def stage_release(repo_root: Path, manifest_path: Path, staging_root: Path) -> None:
    manifest = _load_manifest(repo_root, manifest_path)
    sources = _manifest_sources(manifest)
    _validate_manifest_source_kinds(
        manifest,
        lambda path: _live_manifest_path_kind(repo_root, path),
    )
    release_manifest_payload = _release_manifest_payload(manifest)
    for source in sources:
        if source == RELEASE_MANIFEST:
            continue
        _copy_source(repo_root, staging_root, source)
    release_manifest_path = staging_root / RELEASE_MANIFEST
    release_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    release_manifest_path.write_bytes(release_manifest_payload)


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _bounded_git_output(
    repo_root: Path,
    args: list[str],
    *,
    stdout_limit: int,
    stdout_overflow_error: str,
    stderr_overflow_error: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        process = subprocess.Popen(
            args,
            cwd=repo_root,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, ValueError) as error:
        raise PackageError(f"failed to start Git command: {error}") from error
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    selector: selectors.BaseSelector | None = None
    stdout = bytearray()
    stderr_tail = bytearray()
    stderr_total = 0
    try:
        if stdout_pipe is None or stderr_pipe is None:
            raise PackageError("Git command did not provide capture pipes")
        selector = selectors.DefaultSelector()
        selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
        selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            for key, _events in selector.select():
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == "stdout":
                    if len(stdout) + len(chunk) > stdout_limit:
                        _terminate_and_reap(process)
                        raise PackageError(stdout_overflow_error)
                    stdout.extend(chunk)
                    continue
                stderr_total += len(chunk)
                if stderr_total > GIT_INVENTORY_LIMIT_BYTES:
                    _terminate_and_reap(process)
                    raise PackageError(stderr_overflow_error)
                stderr_tail.extend(chunk)
                if len(stderr_tail) > GIT_ERROR_TAIL_BYTES:
                    del stderr_tail[:-GIT_ERROR_TAIL_BYTES]
        returncode = process.wait()
    except BaseException:
        _terminate_and_reap(process)
        raise
    finally:
        try:
            if selector is not None:
                selector.close()
        finally:
            if stdout_pipe is not None:
                stdout_pipe.close()
            if stderr_pipe is not None:
                stderr_pipe.close()
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr_tail),
    )


def _bounded_git_inventory(
    repo_root: Path,
    args: list[str],
    description: str,
) -> subprocess.CompletedProcess[bytes]:
    return _bounded_git_output(
        repo_root,
        args,
        stdout_limit=GIT_INVENTORY_LIMIT_BYTES,
        stdout_overflow_error=(
            f"{description} exceeds the 16 MiB safety limit"
        ),
        stderr_overflow_error=(
            f"{description} stderr exceeds the 16 MiB safety limit"
        ),
    )


def _bounded_git_untracked_inventory(
    repo_root: Path,
    pathspecs: list[Path],
) -> subprocess.CompletedProcess[bytes]:
    args = [
        "git",
        "--literal-pathspecs",
        "ls-files",
        "--others",
        "-z",
        "--",
        *(path.as_posix() for path in pathspecs),
    ]
    return _bounded_git_inventory(
        repo_root,
        args,
        "untracked manifest-source inventory",
    )


def _bounded_git_index_inventory(
    repo_root: Path,
    pathspecs: list[Path],
) -> subprocess.CompletedProcess[bytes]:
    args = [
        "git",
        "--literal-pathspecs",
        "ls-files",
        "--stage",
        "-z",
        "--",
        *(path.as_posix() for path in pathspecs),
    ]
    return _bounded_git_inventory(
        repo_root,
        args,
        "indexed manifest-source inventory",
    )


def _bounded_git_tree_inventory(
    repo_root: Path,
    sha: str,
    pathspecs: list[Path],
) -> subprocess.CompletedProcess[bytes]:
    args = [
        "git",
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        sha,
        "--",
        *(path.as_posix() for path in pathspecs),
    ]
    return _bounded_git_inventory(
        repo_root,
        args,
        "commit manifest-source inventory",
    )


def _parse_index_inventory(
    result: subprocess.CompletedProcess[bytes],
) -> dict[Path, set[_IndexEntry]]:
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to inspect indexed manifest sources: {stderr}")
    entries: dict[Path, set[_IndexEntry]] = {}
    seen_raw_paths: set[bytes] = set()
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise PackageError("malformed indexed manifest-source inventory")
        if raw_path in seen_raw_paths:
            raise PackageError(
                "duplicate indexed manifest-source path: "
                + raw_path.decode("utf-8", errors="replace")
            )
        seen_raw_paths.add(raw_path)
        fields = metadata.split(b" ")
        if len(fields) != 3:
            raise PackageError("malformed indexed manifest-source metadata")
        mode, object_id, stage = fields
        path = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        entries.setdefault(path, set()).add(_IndexEntry(mode, object_id, stage))
    return entries


def _parse_tree_inventory(
    result: subprocess.CompletedProcess[bytes],
) -> dict[Path, _TreeEntry]:
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to inspect committed manifest sources: {stderr}")
    entries: dict[Path, _TreeEntry] = {}
    seen_raw_paths: set[bytes] = set()
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise PackageError("malformed committed manifest-source inventory")
        if raw_path in seen_raw_paths:
            raise PackageError(
                "duplicate committed manifest-source path: "
                + raw_path.decode("utf-8", errors="replace")
            )
        seen_raw_paths.add(raw_path)
        fields = metadata.split(b" ")
        if len(fields) != 3:
            raise PackageError("malformed committed manifest-source metadata")
        mode, object_type, object_id = fields
        path = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        entry = _TreeEntry(mode, object_type, object_id)
        previous = entries.setdefault(path, entry)
        if previous != entry:
            raise PackageError(f"conflicting committed entries for manifest source: {path}")
    return entries


def _git_blob_size(
    repo_root: Path,
    object_id: bytes,
    label: str,
    *,
    max_bytes: int,
) -> int:
    try:
        object_name = object_id.decode("ascii")
    except UnicodeDecodeError as error:
        raise PackageError(f"committed {label} has an invalid object id") from error
    size_result = _bounded_git_output(
        repo_root,
        ["git", "cat-file", "-s", object_name],
        stdout_limit=GIT_OBJECT_SIZE_OUTPUT_LIMIT_BYTES,
        stdout_overflow_error=f"committed {label} has an invalid blob size",
        stderr_overflow_error=f"committed {label} size stderr exceeds the safety limit",
    )
    if size_result.returncode != 0:
        stderr = size_result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to inspect committed {label} size: {stderr}")
    raw_size = size_result.stdout.strip()
    if not raw_size.isdigit():
        raise PackageError(f"committed {label} has an invalid blob size")
    blob_size = int(raw_size)
    if blob_size > max_bytes:
        raise PackageError(f"committed {label} exceeds {max_bytes} bytes")
    return blob_size


def _read_git_blob(
    repo_root: Path,
    object_id: bytes,
    label: str,
    *,
    max_bytes: int,
) -> bytes:
    try:
        object_name = object_id.decode("ascii")
    except UnicodeDecodeError as error:
        raise PackageError(f"committed {label} has an invalid object id") from error
    blob_size = _git_blob_size(
        repo_root,
        object_id,
        label,
        max_bytes=max_bytes,
    )

    result = _bounded_git_output(
        repo_root,
        ["git", "cat-file", "blob", object_name],
        stdout_limit=blob_size,
        stdout_overflow_error=f"committed {label} exceeded its declared blob size",
        stderr_overflow_error=f"committed {label} stderr exceeds the safety limit",
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to read committed {label}: {stderr}")
    if len(result.stdout) != blob_size:
        raise PackageError(
            f"committed {label} length does not match its declared blob size"
        )
    return result.stdout


def _copy_git_blob(repo_root: Path, snapshot_file: _SnapshotFile, destination: Path) -> None:
    if snapshot_file.size is None:
        raise PackageError(
            f"strict snapshot file has no validated size: {snapshot_file.path}"
        )
    try:
        object_name = snapshot_file.object_id.decode("ascii")
    except UnicodeDecodeError as error:
        raise PackageError(
            f"committed manifest source {snapshot_file.path} has an invalid object id"
        ) from error
    result = _bounded_git_output(
        repo_root,
        ["git", "cat-file", "blob", object_name],
        stdout_limit=snapshot_file.size,
        stdout_overflow_error=(
            f"staging {snapshot_file.path} exceeded its declared blob size"
        ),
        stderr_overflow_error=(
            f"staging {snapshot_file.path} stderr exceeds the safety limit"
        ),
    )
    if result.returncode != 0:
        error_text = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(
            f"failed to stage committed blob {snapshot_file.path}: {error_text}"
        )
    if len(result.stdout) != snapshot_file.size:
        raise PackageError(
            f"staged committed blob {snapshot_file.path} length does not match "
            f"its declared blob size: {len(result.stdout)} != {snapshot_file.size}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as output_file:
            written = output_file.write(result.stdout)
            if written != snapshot_file.size:
                raise PackageError(
                    f"failed to write the complete staged blob {snapshot_file.path}"
                )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    destination.chmod(0o755 if snapshot_file.mode == b"100755" else 0o644)


def _inventory_pathspec_roots(paths: list[Path]) -> list[Path]:
    roots: set[Path] = set()
    for path in paths:
        if not path.parts:
            raise PackageError("manifest source path must not be empty")
        roots.add(Path(path.parts[0]))
    ordered = sorted(roots, key=Path.as_posix)
    encoded_bytes = sum(
        len(os.fsencode(path.as_posix())) + 1
        for path in ordered
    )
    if encoded_bytes > GIT_PATHSPEC_ARG_BUDGET_BYTES:
        return []
    return ordered


def _strict_path_prefix_index(paths: Iterable[Path]) -> _StrictPathIndexNode:
    root = _StrictPathIndexNode()
    for path in paths:
        node = root
        for part in path.parts:
            node = node.children.setdefault(part, _StrictPathIndexNode())
        node.terminal = path
    return root


def _strict_inventory_path_is_selected(
    path: Path,
    selected_index: _StrictPathIndexNode,
) -> bool:
    node = selected_index
    for part in path.parts:
        if node.terminal is not None:
            return True
        node = node.children.get(part)
        if node is None:
            return False
    return node.terminal is not None or bool(node.children)


def _strict_inventory_path_is_within_selected(
    path: Path,
    selected_index: _StrictPathIndexNode,
) -> bool:
    node = selected_index
    for part in path.parts:
        node = node.children.get(part)
        if node is None:
            return False
        if node.terminal is not None:
            return True
    return False


def _strict_inventory_selected_ancestors(
    path: Path,
    selected_index: _StrictPathIndexNode,
    *,
    proper: bool,
) -> tuple[Path, ...]:
    node = selected_index
    matches: list[Path] = []
    last_index = len(path.parts) - 1
    for index, part in enumerate(path.parts):
        node = node.children.get(part)
        if node is None:
            break
        if node.terminal is not None and (not proper or index < last_index):
            matches.append(node.terminal)
    return tuple(matches)


def _validate_strict_worktree(repo_root: Path, selected_paths: list[Path]) -> None:
    pathspecs = _inventory_pathspec_roots(selected_paths)
    result = _bounded_git_inventory(
        repo_root,
        [
            "git",
            "--literal-pathspecs",
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--name-only",
            "-z",
            "--",
            *(path.as_posix() for path in pathspecs),
        ],
        "strict release diff inventory",
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to compare strict release inputs: {stderr}")
    selected_index = _strict_path_prefix_index(selected_paths)
    changed_paths = [
        Path(raw.decode("utf-8", errors="surrogateescape"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]
    if any(
        _strict_inventory_path_is_selected(path, selected_index)
        for path in changed_paths
    ):
        raise PackageError(
            "manifest source worktree differs from the release commit"
        )


def _validate_strict_sha(repo_root: Path, sha: str) -> None:
    if FULL_SHA_RE.fullmatch(sha) is None:
        raise PackageError("strict release SHA must be 40 lowercase hexadecimal characters")
    result = _bounded_git_output(
        repo_root,
        ["git", "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
        stdout_limit=GIT_OBJECT_SIZE_OUTPUT_LIMIT_BYTES,
        stdout_overflow_error="repository HEAD identity exceeds the safety limit",
        stderr_overflow_error="repository HEAD stderr exceeds the safety limit",
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to resolve repository HEAD: {stderr}")
    try:
        head_sha = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise PackageError("repository HEAD returned an invalid identity") from error
    if FULL_SHA_RE.fullmatch(head_sha) is None:
        raise PackageError("repository HEAD returned an invalid identity")
    if sha != head_sha:
        raise PackageError(f"strict release SHA {sha} does not match HEAD {head_sha}")


def _validated_strict_entries(
    repo_root: Path,
    sha: str,
    selected_paths: list[Path],
) -> tuple[dict[Path, set[_IndexEntry]], dict[Path, _TreeEntry]]:
    inventory_pathspecs = _inventory_pathspec_roots(selected_paths)
    index_entries = _parse_index_inventory(
        _bounded_git_index_inventory(repo_root, inventory_pathspecs)
    )
    tree_entries = _parse_tree_inventory(
        _bounded_git_tree_inventory(repo_root, sha, inventory_pathspecs)
    )
    selected_path_index = _strict_path_prefix_index(selected_paths)
    selected_index = {
        path: entries
        for path, entries in index_entries.items()
        if _strict_inventory_path_is_selected(
            path,
            selected_path_index,
        )
    }
    selected_tree = {
        path: entry
        for path, entry in tree_entries.items()
        if _strict_inventory_path_is_selected(
            path,
            selected_path_index,
        )
    }

    gitlinks = sorted(
        {
            path
            for path, entries in selected_index.items()
            if any(entry.mode == b"160000" for entry in entries)
        }
        | {
            path for path, entry in selected_tree.items() if entry.mode == b"160000"
        },
        key=Path.as_posix,
    )
    if gitlinks:
        sample = ", ".join(path.as_posix() for path in gitlinks[:20])
        suffix = "" if len(gitlinks) <= 20 else ", ..."
        raise PackageError(
            "refusing gitlink entries under manifest sources: " + sample + suffix
        )

    symlinks = sorted(
        {
            path
            for path, entries in selected_index.items()
            if any(entry.mode == b"120000" for entry in entries)
        }
        | {path for path, entry in selected_tree.items() if entry.mode == b"120000"},
        key=Path.as_posix,
    )
    if symlinks:
        sample = ", ".join(path.as_posix() for path in symlinks[:20])
        suffix = "" if len(symlinks) <= 20 else ", ..."
        raise PackageError(
            "refusing symlink entries under manifest sources: " + sample + suffix
        )

    nested_git_paths = sorted(
        {
            path
            for path in (*selected_index.keys(), *selected_tree.keys())
            if ".git" in path.parts
        },
        key=Path.as_posix,
    )
    if nested_git_paths:
        sample = ", ".join(path.as_posix() for path in nested_git_paths[:20])
        suffix = "" if len(nested_git_paths) <= 20 else ", ..."
        raise PackageError(
            "refusing nested Git repository under manifest sources: " + sample + suffix
        )

    invalid_tree_entries = [
        path
        for path, entry in selected_tree.items()
        if entry.mode not in REGULAR_GIT_MODES or entry.object_type != b"blob"
    ]
    if invalid_tree_entries:
        sample = ", ".join(path.as_posix() for path in invalid_tree_entries[:20])
        suffix = "" if len(invalid_tree_entries) <= 20 else ", ..."
        raise PackageError("unsupported committed manifest-source entries: " + sample + suffix)

    mismatched_paths = sorted(
        [
            path
            for path in selected_index.keys() | selected_tree.keys()
            if selected_index.get(path, set())
            != (
                {
                    _IndexEntry(
                        selected_tree[path].mode,
                        selected_tree[path].object_id,
                        b"0",
                    )
                }
                if path in selected_tree
                else set()
            )
        ],
        key=Path.as_posix,
    )
    if mismatched_paths:
        sample = ", ".join(path.as_posix() for path in mismatched_paths[:20])
        suffix = "" if len(mismatched_paths) <= 20 else ", ..."
        raise PackageError(
            "indexed manifest sources differ from the release commit: " + sample + suffix
        )
    return selected_index, selected_tree


def _load_strict_manifest(
    repo_root: Path,
    sha: str,
    manifest_path: Path,
) -> tuple[dict[str, Any], bytes]:
    index_entries, tree_entries = _validated_strict_entries(
        repo_root,
        sha,
        [manifest_path],
    )
    tree_entry = tree_entries.get(manifest_path)
    if tree_entry is None or tree_entry.mode not in REGULAR_GIT_MODES:
        raise PackageError(f"manifest is not a committed regular file: {manifest_path}")
    index_entry = next(iter(index_entries[manifest_path]))
    payload = _read_git_blob(
        repo_root,
        index_entry.object_id,
        f"manifest {manifest_path}",
        max_bytes=MAX_RELEASE_MANIFEST_BYTES,
    )
    return _parse_manifest_bytes(payload, manifest_path), index_entry.mode


def _strict_snapshot_entries(
    sources: list[Path],
    index_entries: dict[Path, set[_IndexEntry]],
    tree_entries: dict[Path, _TreeEntry],
) -> tuple[set[Path], dict[Path, _SnapshotFile]]:
    directories: set[Path] = set()
    files: dict[Path, _SnapshotFile] = {}
    unique_sources = tuple(dict.fromkeys(sources))
    directory_sources = tuple(
        source for source in unique_sources if source not in tree_entries
    )
    directory_source_index = _strict_path_prefix_index(directory_sources)
    directory_sources_with_descendants: set[Path] = set()
    selected_directory_by_path: dict[Path, Path] = {}
    for path in tree_entries:
        selected_directories = _strict_inventory_selected_ancestors(
            path,
            directory_source_index,
            proper=True,
        )
        if selected_directories:
            directory_sources_with_descendants.update(selected_directories)
            selected_directory_by_path[path] = selected_directories[-1]

    for source in unique_sources:
        exact_entry = tree_entries.get(source)
        if (
            exact_entry is None
            and source not in directory_sources_with_descendants
        ):
            raise PackageError(f"manifest source is missing from release commit: {source}")
        is_directory = exact_entry is None
        if _is_generated_path(source, is_dir=is_directory):
            raise PackageError(f"refusing generated manifest source: {source}")
        if source == RELEASE_MANIFEST:
            continue
        if exact_entry is not None:
            index_entry = next(iter(index_entries[source]))
            files[source] = _SnapshotFile(
                source,
                index_entry.mode,
                index_entry.object_id,
            )
            continue

        directories.add(source)

    for path, source in selected_directory_by_path.items():
        relative_path = path.relative_to(source)
        for parent in path.parents:
            if parent == source:
                break
            relative_parent = parent.relative_to(source)
            if not _is_generated_path(relative_parent, is_dir=True):
                directories.add(parent)
        if _is_generated_path(relative_path, is_dir=False):
            continue
        index_entry = next(iter(index_entries[path]))
        files[path] = _SnapshotFile(path, index_entry.mode, index_entry.object_id)
    return directories, files


def _portable_release_path_key(path: Path) -> tuple[str, ...]:
    try:
        path.as_posix().encode("utf-8")
    except UnicodeEncodeError as error:
        raise PackageError(f"strict release snapshot path is not valid UTF-8: {path}") from error
    return tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in path.parts
    )


def _validate_portable_release_paths(
    directories: set[Path],
    files: set[Path],
) -> None:
    portable_entries: dict[tuple[str, ...], tuple[Path, str]] = {}

    def register(path: Path, kind: str) -> None:
        key = _portable_release_path_key(path)
        previous = portable_entries.get(key)
        if previous is None:
            portable_entries[key] = (path, kind)
            return
        previous_path, previous_kind = previous
        if previous_path != path or previous_kind != kind:
            raise PackageError(
                "portable path conflict in release archive: "
                f"{previous_path} ({previous_kind}) conflicts with {path} ({kind})"
            )

    def register_directory(path: Path) -> None:
        for length in range(1, len(path.parts) + 1):
            register(Path(*path.parts[:length]), "directory")

    for directory in sorted(directories, key=Path.as_posix):
        register_directory(directory)
    for file_path in sorted(files, key=Path.as_posix):
        if len(file_path.parts) > 1:
            register_directory(Path(*file_path.parts[:-1]))
        register(file_path, "file")


def _strict_snapshot_path_kinds(
    tree_entries: dict[Path, _TreeEntry],
) -> dict[Path, str]:
    kinds: dict[Path, str] = {}
    for path, entry in tree_entries.items():
        if entry.mode in REGULAR_GIT_MODES:
            kinds[path] = "file"
        for parent in path.parents:
            if parent == Path("."):
                break
            kinds.setdefault(parent, "directory")
    return kinds


def _release_archive_member_paths(
    package_name: str,
    directories: set[Path],
    files: set[Path],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    archive_files = set(files)
    archive_directories = set(directories)
    for path in (*directories, *archive_files):
        for parent in path.parents:
            if parent == Path("."):
                break
            archive_directories.add(parent)

    member_count = 1 + len(archive_directories) + len(archive_files)
    if member_count > MAX_ARCHIVE_MEMBERS:
        raise PackageError(
            f"release archive exceeds {MAX_ARCHIVE_MEMBERS} member limit: "
            f"{member_count}"
        )

    def validate(relative_path: Path | None) -> None:
        member_name = package_name
        if relative_path is not None:
            member_name = f"{package_name}/{relative_path.as_posix()}"
        if len(member_name) > MAX_ARCHIVE_MEMBER_PATH_BYTES:
            raise PackageError(
                "release archive member path exceeds UTF-8 byte limit: "
                f"> {MAX_ARCHIVE_MEMBER_PATH_BYTES}"
            )
        try:
            encoded_name = member_name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise PackageError(
                "release archive member path is not valid UTF-8"
            ) from error
        if len(encoded_name) > MAX_ARCHIVE_MEMBER_PATH_BYTES:
            raise PackageError(
                "release archive member path exceeds UTF-8 byte limit: "
                f"{len(encoded_name)} > {MAX_ARCHIVE_MEMBER_PATH_BYTES}"
            )
        parts = member_name.split("/")
        if len(parts) > MAX_ARCHIVE_MEMBER_PATH_DEPTH:
            raise PackageError(
                "release archive member path exceeds depth limit: "
                f"{len(parts)} > {MAX_ARCHIVE_MEMBER_PATH_DEPTH}"
            )
        for index, part in enumerate(parts, start=1):
            component_bytes = len(part.encode("utf-8"))
            if component_bytes > MAX_ARCHIVE_MEMBER_COMPONENT_BYTES:
                raise PackageError(
                    "release archive member path component exceeds UTF-8 "
                    f"byte limit: component {index}: {component_bytes} > "
                    f"{MAX_ARCHIVE_MEMBER_COMPONENT_BYTES}"
                )

    validate(None)
    ordered_directories = tuple(sorted(archive_directories, key=Path.as_posix))
    ordered_files = tuple(sorted(archive_files, key=Path.as_posix))
    for relative_path in (*ordered_directories, *ordered_files):
        validate(relative_path)
    return ordered_directories, ordered_files


def _bind_strict_snapshot_file_sizes(
    repo_root: Path,
    files: dict[Path, _SnapshotFile],
    manifest_payload: bytes,
) -> dict[Path, _SnapshotFile]:
    expanded_bytes = len(manifest_payload)
    if expanded_bytes > MAX_ARCHIVE_MEMBER_BYTES:
        raise PackageError(
            "generated release manifest exceeds archive member byte limit: "
            f"{expanded_bytes} > {MAX_ARCHIVE_MEMBER_BYTES}"
        )
    if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
        raise PackageError(
            "strict release archive exceeds total expanded file byte limit: "
            f"{expanded_bytes} > {MAX_ARCHIVE_EXPANDED_BYTES}"
        )

    sized_files: dict[Path, _SnapshotFile] = {}
    size_by_object_id: dict[bytes, int] = {}
    for path in sorted(files, key=Path.as_posix):
        snapshot_file = files[path]
        size = size_by_object_id.get(snapshot_file.object_id)
        if size is None:
            size = _git_blob_size(
                repo_root,
                snapshot_file.object_id,
                f"manifest source {path}",
                max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
            )
            size_by_object_id[snapshot_file.object_id] = size
        expanded_bytes += size
        if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise PackageError(
                "strict release archive exceeds total expanded file byte limit: "
                f"{expanded_bytes} > {MAX_ARCHIVE_EXPANDED_BYTES}"
            )
        sized_files[path] = _SnapshotFile(
            path=snapshot_file.path,
            mode=snapshot_file.mode,
            object_id=snapshot_file.object_id,
            size=size,
        )
    return sized_files


def _tar_member_serialized_size(
    member_name: str,
    *,
    is_directory: bool,
    size: int = 0,
    mode: int = 0o644,
) -> int:
    header_name = f"{member_name}/" if is_directory else member_name
    tarinfo = tarfile.TarInfo(header_name)
    tarinfo.type = tarfile.DIRTYPE if is_directory else tarfile.REGTYPE
    tarinfo.size = 0 if is_directory else size
    tarinfo.mode = 0o755 if is_directory else mode
    tarinfo = _tar_filter(tarinfo)
    try:
        header = tarinfo.tobuf(
            format=tarfile.PAX_FORMAT,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except (KeyError, UnicodeError, ValueError) as error:
        raise PackageError(
            f"failed to size strict release archive member {member_name}: {error}"
        ) from error
    padded_payload_size = ((tarinfo.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    return len(header) + padded_payload_size


def _validate_release_archive_stream_size(
    package_name: str,
    archive_directories: tuple[Path, ...],
    archive_files: tuple[Path, ...],
    file_metadata: dict[Path, tuple[int, int]],
) -> None:
    stream_bytes = _tar_member_serialized_size(
        package_name,
        is_directory=True,
    )
    for directory in archive_directories:
        stream_bytes += _tar_member_serialized_size(
            f"{package_name}/{directory.as_posix()}",
            is_directory=True,
        )
    for path in archive_files:
        size, mode = file_metadata[path]
        stream_bytes += _tar_member_serialized_size(
            f"{package_name}/{path.as_posix()}",
            is_directory=False,
            size=size,
            mode=mode,
        )

    stream_bytes += tarfile.BLOCKSIZE * 2
    stream_bytes = (
        (stream_bytes + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE
    ) * tarfile.RECORDSIZE
    if stream_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
        raise PackageError(
            "release archive exceeds total expanded tar stream limit: "
            f"{stream_bytes} > {MAX_ARCHIVE_EXPANDED_BYTES}"
        )


def _prepare_strict_release_snapshot(
    repo_root: Path,
    manifest_path: Path,
    sha: str,
) -> StrictReleaseSnapshot:
    _validate_strict_sha(repo_root, sha)
    manifest_absolute, manifest_mode = _validated_repo_path(
        repo_root,
        manifest_path,
        "manifest",
    )
    if not stat.S_ISREG(manifest_mode):
        raise PackageError(f"manifest is not a regular file: {manifest_path}")
    del manifest_absolute
    _reject_nested_git_marker_ancestors(repo_root, manifest_path, "manifest")

    manifest, manifest_mode = _load_strict_manifest(repo_root, sha, manifest_path)
    sources = _manifest_sources(manifest)
    selected_paths = [manifest_path, *sources]
    for source in sources:
        _validated_repo_path(repo_root, source, "manifest source")
        _reject_nested_git_marker_ancestors(repo_root, source, "manifest source")

    index_entries, tree_entries = _validated_strict_entries(
        repo_root,
        sha,
        selected_paths,
    )
    strict_path_kinds = _strict_snapshot_path_kinds(tree_entries)
    _validate_manifest_source_kinds(manifest, strict_path_kinds.get)
    directories, files = _strict_snapshot_entries(
        sources,
        index_entries,
        tree_entries,
    )
    if RELEASE_MANIFEST in files:
        raise PackageError(
            "strict release snapshot path conflicts with generated manifest: "
            f"{RELEASE_MANIFEST}"
        )
    release_files = {*files, RELEASE_MANIFEST}
    _validate_portable_release_paths(directories, release_files)
    package_name = f"personal-codex-{sha}"
    archive_directories, archive_files = _release_archive_member_paths(
        package_name,
        directories,
        release_files,
    )

    selected_path_index = _strict_path_prefix_index(selected_paths)
    result = _bounded_git_untracked_inventory(
        repo_root,
        _inventory_pathspec_roots(selected_paths),
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to inspect untracked manifest sources: {stderr}")
    untracked = [
        Path(raw.decode("utf-8", errors="surrogateescape"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]
    packaged_untracked = [
        path
        for path in untracked
        if _strict_inventory_path_is_selected(path, selected_path_index)
        and not _is_generated_path(path, is_dir=False)
    ]
    if packaged_untracked:
        sample = ", ".join(path.as_posix() for path in packaged_untracked[:20])
        suffix = "" if len(packaged_untracked) <= 20 else ", ..."
        raise PackageError(
            "refusing untracked files under manifest sources: " + sample + suffix
        )
    _validate_strict_worktree(repo_root, selected_paths)
    manifest_payload = _release_manifest_payload(manifest)
    files = _bind_strict_snapshot_file_sizes(
        repo_root,
        files,
        manifest_payload,
    )
    file_metadata = {
        RELEASE_MANIFEST: (
            len(manifest_payload),
            0o755 if manifest_mode == b"100755" else 0o644,
        )
    }
    for path, snapshot_file in files.items():
        assert snapshot_file.size is not None
        file_metadata[path] = (
            snapshot_file.size,
            0o755 if snapshot_file.mode == b"100755" else 0o644,
        )
    _validate_release_archive_stream_size(
        package_name,
        archive_directories,
        archive_files,
        file_metadata,
    )

    return StrictReleaseSnapshot(
        manifest=manifest,
        manifest_mode=manifest_mode,
        directories=tuple(sorted(directories, key=Path.as_posix)),
        files=tuple(files[path] for path in sorted(files, key=Path.as_posix)),
        manifest_payload=manifest_payload,
    )


def stage_strict_release(
    repo_root: Path,
    snapshot: StrictReleaseSnapshot,
    staging_root: Path,
) -> None:
    _manifest_sources(snapshot.manifest)
    release_manifest_payload = snapshot.manifest_payload
    if release_manifest_payload is None:
        release_manifest_payload = _release_manifest_payload(snapshot.manifest)
    for directory in snapshot.directories:
        (staging_root / directory).mkdir(parents=True, exist_ok=True)
    for snapshot_file in snapshot.files:
        _copy_git_blob(repo_root, snapshot_file, staging_root / snapshot_file.path)
    release_manifest_path = staging_root / RELEASE_MANIFEST
    release_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    release_manifest_path.write_bytes(release_manifest_payload)
    release_manifest_path.chmod(0o755 if snapshot.manifest_mode == b"100755" else 0o644)


def ensure_manifest_sources_are_strictly_tracked(
    repo_root: Path,
    manifest_path: Path,
    *,
    expected_sha: str | None = None,
) -> StrictReleaseSnapshot | None:
    if expected_sha is not None:
        return _prepare_strict_release_snapshot(repo_root, manifest_path, expected_sha)

    manifest = _load_manifest(repo_root, manifest_path)
    sources = _manifest_sources(manifest)
    pathspecs = [manifest_path, *sources]
    for source in sources:
        _source_path, source_mode = _validated_repo_path(
            repo_root,
            source,
            "manifest source",
        )
        if _is_generated_path(source, is_dir=stat.S_ISDIR(source_mode)):
            raise PackageError(f"refusing generated manifest source: {source}")
        if not (stat.S_ISREG(source_mode) or stat.S_ISDIR(source_mode)):
            raise PackageError(f"unsupported manifest source type: {source}")
    inventory_pathspecs = _inventory_pathspec_roots(pathspecs)
    index_result = _bounded_git_index_inventory(
        repo_root,
        inventory_pathspecs,
    )
    if index_result.returncode != 0:
        stderr = index_result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to inspect indexed manifest sources: {stderr}")
    parsed_index_entries = _parse_index_inventory(index_result)
    index_entries = {
        path: {(entry.mode, entry.stage) for entry in entries}
        for path, entries in parsed_index_entries.items()
    }
    gitlinks = [
        path
        for path, entries in parsed_index_entries.items()
        if any(entry.mode == b"160000" for entry in entries)
    ]
    selected_index = _strict_path_prefix_index(pathspecs)
    selected_gitlinks = [
        gitlink
        for gitlink in gitlinks
        if _strict_inventory_path_is_selected(
            gitlink,
            selected_index,
        )
    ]
    if selected_gitlinks:
        sample = ", ".join(path.as_posix() for path in selected_gitlinks[:20])
        suffix = "" if len(selected_gitlinks) <= 20 else ", ..."
        raise PackageError(
            "refusing gitlink entries under manifest sources: " + sample + suffix
        )

    result = _bounded_git_untracked_inventory(
        repo_root,
        inventory_pathspecs,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PackageError(f"failed to inspect untracked manifest sources: {stderr}")
    untracked = [
        Path(raw.decode("utf-8", errors="surrogateescape"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]
    packaged_untracked = [
        path
        for path in untracked
        if _strict_inventory_path_is_selected(path, selected_index)
        and not _is_generated_path(path, is_dir=False)
    ]
    if packaged_untracked:
        sample = ", ".join(path.as_posix() for path in packaged_untracked[:20])
        suffix = "" if len(packaged_untracked) <= 20 else ", ..."
        raise PackageError(
            "refusing untracked files under manifest sources: " + sample + suffix
        )

    packaged_source_index = _strict_path_prefix_index(
        source for source in sources if source != RELEASE_MANIFEST
    )
    selected_index_paths = {
        path
        for path in index_entries
        if _strict_inventory_path_is_within_selected(
            path,
            packaged_source_index,
        )
        and not _is_generated_path(path, is_dir=False)
    }
    packaged_files = sorted(
        {manifest_path, *selected_index_paths},
        key=Path.as_posix,
    )
    invalid_index_paths = [
        path
        for path in packaged_files
        if index_entries.get(path) not in ({(b"100644", b"0")}, {(b"100755", b"0")})
    ]
    if invalid_index_paths:
        sample = ", ".join(path.as_posix() for path in invalid_index_paths[:20])
        suffix = "" if len(invalid_index_paths) <= 20 else ", ..."
        raise PackageError(
            "packaged files require one stage-0 regular index entry: " + sample + suffix
        )
    return None


def _is_generated_path(path: Path, *, is_dir: bool | None = None) -> bool:
    if any(part in GENERATED_DIR_NAMES for part in path.parts):
        return True
    if path.name in GENERATED_FILE_NAMES:
        return True
    if is_dir is True:
        return False
    return path.suffix in GENERATED_SUFFIXES


def _iter_tar_paths(root: Path) -> list[Path]:
    paths = [
        path
        for path in root.rglob("*")
        if not _is_generated_path(
            path.relative_to(root),
            is_dir=stat.S_ISDIR(path.lstat().st_mode),
        )
    ]
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _staged_release_inventory(
    staging_root: Path,
) -> tuple[set[Path], dict[Path, tuple[int, int]]]:
    directories: set[Path] = set()
    file_metadata: dict[Path, tuple[int, int]] = {}
    for path in _iter_tar_paths(staging_root):
        relative_path = path.relative_to(staging_root)
        try:
            metadata = path.lstat()
        except (OSError, ValueError) as error:
            raise PackageError(
                f"failed to inspect staged release member {relative_path}: {error}"
            ) from error
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(relative_path)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PackageError(
                f"refusing unsupported staged release member: {relative_path}"
            )
        file_metadata[relative_path] = (metadata.st_size, metadata.st_mode)
    return directories, file_metadata


def _validate_staged_release_compatibility(
    staging_root: Path,
    package_name: str,
) -> None:
    directories, file_metadata = _staged_release_inventory(staging_root)
    release_files = set(file_metadata)
    _validate_portable_release_paths(directories, release_files)
    archive_directories, archive_files = _release_archive_member_paths(
        package_name,
        directories,
        release_files,
    )

    expanded_file_bytes = 0
    for path in archive_files:
        size, _mode = file_metadata[path]
        member_name = f"{package_name}/{path.as_posix()}"
        if size < 0 or size > MAX_ARCHIVE_MEMBER_BYTES:
            raise PackageError(
                "release archive member exceeds expanded byte limit: "
                f"{member_name}: {size} > {MAX_ARCHIVE_MEMBER_BYTES}"
            )
        expanded_file_bytes += size
        if expanded_file_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise PackageError(
                "release archive exceeds total expanded file byte limit: "
                f"{expanded_file_bytes} > {MAX_ARCHIVE_EXPANDED_BYTES}"
            )

    _validate_release_archive_stream_size(
        package_name,
        archive_directories,
        archive_files,
        file_metadata,
    )


def _tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = ""
    tarinfo.gname = ""
    tarinfo.mtime = 0
    if tarinfo.isdir():
        tarinfo.mode = 0o755
    elif tarinfo.mode & 0o111:
        tarinfo.mode = 0o755
    else:
        tarinfo.mode = 0o644
    return tarinfo


class _BoundedArchiveWriter:
    def __init__(
        self,
        writer: Any,
        maximum_bytes: int,
        *,
        overflow_description: str = "release archive exceeds total expanded tar stream limit",
        defer_overflow: bool = False,
    ) -> None:
        self._writer = writer
        self._maximum_bytes = maximum_bytes
        self._overflow_description = overflow_description
        self._defer_overflow = defer_overflow
        self._bytes_written = 0
        self._logical_bytes_written = 0
        self._overflow_size: int | None = None

    def write(self, payload: bytes) -> int:
        next_size = self._logical_bytes_written + len(payload)
        self._logical_bytes_written = next_size
        if next_size > self._maximum_bytes:
            if self._defer_overflow:
                remaining = max(self._maximum_bytes - self._bytes_written, 0)
                if remaining:
                    written = self._writer.write(payload[:remaining])
                    if written != remaining:
                        raise PackageError(
                            "failed to write the bounded release archive stream"
                        )
                    self._bytes_written += written
                self._overflow_size = max(self._overflow_size or 0, next_size)
                return len(payload)
            raise PackageError(
                f"{self._overflow_description}: "
                f"{next_size} > {self._maximum_bytes}"
            )
        written = self._writer.write(payload)
        if written != len(payload):
            raise PackageError(
                "failed to write the complete uncompressed release archive stream"
            )
        self._bytes_written = next_size
        return written

    def raise_if_overflow(self) -> None:
        if self._overflow_size is not None:
            raise PackageError(
                f"{self._overflow_description}: "
                f"{self._overflow_size} > {self._maximum_bytes}"
            )

    def tell(self) -> int:
        return self._bytes_written

    def flush(self) -> None:
        self._writer.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)


def create_archive(staging_root: Path, archive_path: Path, package_name: str) -> None:
    _validate_staged_release_compatibility(staging_root, package_name)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with archive_path.open("wb") as raw_file:
            compressed_writer = _BoundedArchiveWriter(
                raw_file,
                MAX_ARCHIVE_COMPRESSED_BYTES,
                overflow_description="release archive exceeds compressed size limit",
                defer_overflow=True,
            )
            with gzip.GzipFile(
                fileobj=compressed_writer,
                mode="wb",
                mtime=0,
            ) as gzip_file:
                bounded_writer = _BoundedArchiveWriter(
                    gzip_file,
                    MAX_ARCHIVE_EXPANDED_BYTES,
                )
                with tarfile.open(
                    fileobj=bounded_writer,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    archive.add(
                        staging_root,
                        arcname=package_name,
                        recursive=False,
                        filter=_tar_filter,
                    )
                    for path in _iter_tar_paths(staging_root):
                        relative_path = path.relative_to(staging_root).as_posix()
                        archive.add(
                            path,
                            arcname=f"{package_name}/{relative_path}",
                            recursive=False,
                            filter=_tar_filter,
                        )
            compressed_writer.raise_if_overflow()
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise


def _checksum_path(archive_path: Path) -> Path:
    checksum_path = archive_path.parent / archive_path.name.removesuffix(".tar.gz")
    return checksum_path.with_suffix(".sha256")


def write_checksum(archive_path: Path) -> Path:
    digest = hashlib.sha256()
    with archive_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum_path = _checksum_path(archive_path)
    checksum_path.write_text(
        f"{digest.hexdigest()}  {archive_path.name}\n",
        encoding="utf-8",
    )
    return checksum_path


def build_package(
    repo_root: Path,
    manifest_path: Path,
    output_dir: Path,
    sha: str,
    *,
    require_clean_sources: bool = False,
) -> tuple[Path, Path]:
    if FULL_SHA_RE.fullmatch(sha) is None:
        raise PackageError("release SHA must be 40 lowercase hexadecimal characters")
    strict_snapshot: StrictReleaseSnapshot | None = None
    if require_clean_sources:
        strict_snapshot = ensure_manifest_sources_are_strictly_tracked(
            repo_root,
            manifest_path,
            expected_sha=sha,
        )
    package_name = f"personal-codex-{sha}"
    archive_path = output_dir / f"{package_name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="personal-codex-package.") as temp_dir_raw:
        staging_root = Path(temp_dir_raw) / package_name
        staging_root.mkdir(parents=True)
        if strict_snapshot is None:
            stage_release(repo_root, manifest_path, staging_root)
        else:
            stage_strict_release(repo_root, strict_snapshot, staging_root)
        create_archive(staging_root, archive_path, package_name)
    try:
        checksum_path = write_checksum(archive_path)
    except BaseException:
        archive_path.unlink(missing_ok=True)
        _checksum_path(archive_path).unlink(missing_ok=True)
        raise
    return archive_path, checksum_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a personal Codex release package.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument("--sha", required=True)
    parser.add_argument(
        "--require-clean-sources",
        action="store_true",
        help=(
            "Require SHA-matched committed manifest sources and reject untracked, "
            "symlinked, gitlink, or nested-repository content."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest)
    output_dir = Path(args.output_dir)
    archive_path, checksum_path = build_package(
        repo_root,
        manifest_path,
        output_dir,
        args.sha,
        require_clean_sources=args.require_clean_sources,
    )
    print(archive_path)
    print(checksum_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
