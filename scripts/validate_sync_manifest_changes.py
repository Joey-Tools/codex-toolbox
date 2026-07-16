#!/usr/bin/env python3
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
import importlib.util
import io
from itertools import chain
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import sys
import tempfile
from typing import Any, Optional
import unicodedata
from urllib.parse import quote
from urllib.request import Request, urlopen


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
REMOVED_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
KINDS = {"file", "directory", "skill"}
PUBLIC_OWNER = "public"
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
SYNC_INTERNAL_TARGET = PurePosixPath("personal-sync")
PENDING_LINK_POINTER_TARGET = PurePosixPath(
    ".personal-sync-pending-transaction.json"
)
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_RELEASES_PAGE_SIZE = 100
MAX_GITHUB_API_RESPONSE_BYTES = 16 * 1024 * 1024
# Allow a terminal empty page after exactly MAX_GITHUB_RELEASES entries.
MAX_GITHUB_RELEASE_PAGES = 101
MAX_GITHUB_RELEASES = 10_000
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_TAG_RE = re.compile(
    r"^personal-codex-\d{8}-\d{6}-([0-9a-f]{7,40})$"
)
RELEASE_ARCHIVE_ASSET_RE = re.compile(
    r"^personal-codex-([0-9a-f]{40})\.tar\.gz$"
)
RELEASE_CHECKSUM_ASSET_RE = re.compile(
    r"^personal-codex-([0-9a-f]{40})\.sha256$"
)
PUBLISHED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$"
)
MAX_RELEASE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 4300
MAX_GIT_STDERR_BYTES = 1024 * 1024
GIT_ERROR_TAIL_BYTES = 64 * 1024
MAX_GIT_METADATA_BYTES = 64 * 1024
MAX_GIT_RELEASE_HISTORY_BYTES = 1024 * 1024
MAX_RELEASE_MANIFEST_HISTORY_BYTES = 64 * 1024 * 1024
MAX_GIT_CHANGE_SUMMARY_BYTES = 1024 * 1024
MAX_GIT_TREE_LISTING_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_PATH_BYTES = 4096
MAX_MANIFEST_SOURCE_DEPTH = 64
MAX_MANIFEST_TARGET_PATH_BYTES = 4096
MAX_MANIFEST_TARGET_COMPONENT_BYTES = 255
MAX_MANIFEST_TARGET_PATH_DEPTH = 64
MAX_PENDING_LINK_RECORDS = 10_000
MAX_PENDING_LINK_CLAIMS = 20_000
# A first-install transaction also records and claims the owner's current link.
MAX_MANIFEST_ACTIVE_LINKS = (
    min(MAX_PENDING_LINK_RECORDS, MAX_PENDING_LINK_CLAIMS) - 1
)
REGULAR_GIT_MODES = {b"100644", b"100755"}
GIT_OBJECT_ID_RE = re.compile(rb"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ValidationError(RuntimeError):
    pass


_SYNC_RUNTIME_MODULE: Any | None = None
_TRANSITION_CAPACITY_PROFILE_KEY = "_runtime_transition_capacity_profile"


def _sync_runtime_module() -> Any:
    global _SYNC_RUNTIME_MODULE
    if _SYNC_RUNTIME_MODULE is not None:
        return _SYNC_RUNTIME_MODULE
    script_path = Path(__file__).with_name("codex_personal_sync.py")
    module_name = "_codex_personal_sync_capacity_runtime"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ValidationError(f"failed to load sync runtime: {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    _SYNC_RUNTIME_MODULE = module
    return module


def _transition_capacity_profile(model: dict[str, Any]) -> Any:
    profile = model.get(_TRANSITION_CAPACITY_PROFILE_KEY)
    if profile is not None:
        return profile
    runtime = _sync_runtime_module()
    profile = runtime._manifest_transition_capacity_profile(
        model["owner"],
        model["links"],
        model["removed"],
    )
    model[_TRANSITION_CAPACITY_PROFILE_KEY] = profile
    return profile


def _bounded_json_integer(raw_value: str) -> int:
    digits = raw_value[1:] if raw_value.startswith("-") else raw_value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits"
        )
    return int(raw_value)


ManifestPathKind = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class _LiveManifestSnapshot:
    payload: bytes
    root_identity: tuple[int, int, int]
    ancestor_identities: tuple[tuple[str, tuple[int, int, int]], ...]
    file_snapshot: tuple[int, int, int, int, int, int]


class _LiveWorktreePathKind:
    def __init__(
        self,
        resolve: ManifestPathKind,
        verify: Callable[[], None],
    ) -> None:
        self._resolve = resolve
        self._verify = verify

    def __call__(self, raw_path: str) -> str | None:
        return self._resolve(raw_path)

    def verify(self) -> None:
        self._verify()


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


def _bounded_git_output(
    repo_root: Path,
    args: list[str],
    *,
    stdout_limit: int,
    stdout_overflow_error: str,
    env: dict[str, str] | None = None,
    stdin_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    git_environment = os.environ.copy()
    if env is not None:
        git_environment.update(env)
    git_environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    if stdin_data is not None and len(stdin_data) > MAX_GIT_RELEASE_HISTORY_BYTES:
        raise ValidationError("Git command input exceeds the 1 MiB safety limit")
    stdin_file = None
    try:
        if stdin_data is not None:
            stdin_file = tempfile.TemporaryFile()
            stdin_file.write(stdin_data)
            stdin_file.seek(0)
        process = subprocess.Popen(
            args,
            cwd=repo_root,
            env=git_environment,
            stdin=stdin_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, ValueError) as error:
        if stdin_file is not None:
            stdin_file.close()
        raise ValidationError(f"failed to start Git command: {error}") from error
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    selector: selectors.BaseSelector | None = None
    stdout = bytearray()
    stderr_tail = bytearray()
    stderr_total = 0
    try:
        if stdout_pipe is None or stderr_pipe is None:
            raise ValidationError("Git command did not provide capture pipes")
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
                        raise ValidationError(stdout_overflow_error)
                    stdout.extend(chunk)
                    continue
                stderr_total += len(chunk)
                if stderr_total > MAX_GIT_STDERR_BYTES:
                    _terminate_and_reap(process)
                    raise ValidationError(
                        "Git command stderr exceeds the 1 MiB safety limit"
                    )
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
            if stdin_file is not None:
                stdin_file.close()
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr_tail),
    )


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stderr.decode("utf-8", errors="replace").strip()


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
                raise ValidationError(
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


def _relative_path(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValidationError(f"{field} must be a non-empty relative path")
    if "\0" in raw:
        raise ValidationError(f"{field} must not contain embedded NUL")
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValidationError(f"{field} must be valid UTF-8") from error
    raw_parts = raw.split("/")
    if (
        raw.startswith("/")
        or ".." in raw_parts
        or any(part in {"", "."} for part in raw_parts)
    ):
        raise ValidationError(f"{field} must be a safe relative path: {raw}")
    path = PurePosixPath(raw)
    return path.as_posix()


def _target_path(raw: object, field: str) -> str:
    value = _relative_path(raw, field)
    encoded = value.encode("utf-8")
    parts = PurePosixPath(value).parts
    if len(encoded) > MAX_MANIFEST_TARGET_PATH_BYTES:
        raise ValidationError(
            f"{field} exceeds {MAX_MANIFEST_TARGET_PATH_BYTES} UTF-8 bytes"
        )
    if len(parts) > MAX_MANIFEST_TARGET_PATH_DEPTH:
        raise ValidationError(
            f"{field} exceeds {MAX_MANIFEST_TARGET_PATH_DEPTH} path components"
        )
    for index, part in enumerate(parts, start=1):
        component_bytes = len(part.encode("utf-8"))
        if component_bytes > MAX_MANIFEST_TARGET_COMPONENT_BYTES:
            raise ValidationError(
                f"{field} component {index} exceeds "
                f"{MAX_MANIFEST_TARGET_COMPONENT_BYTES} UTF-8 bytes"
            )
    path_key = _portable_target_key(value)
    internal_key = _portable_target_key(SYNC_INTERNAL_TARGET.as_posix())
    if path_key[: len(internal_key)] == internal_key:
        raise ValidationError(f"{field} must not use sync internal path: {value}")
    pointer_key = _portable_target_key(PENDING_LINK_POINTER_TARGET.as_posix())
    if path_key[: len(pointer_key)] == pointer_key:
        raise ValidationError(
            f"{field} must not use sync pending transaction path: {value}"
        )
    return value


def _owner_id(raw: object, field: str) -> str:
    if not isinstance(raw, str) or ID_RE.fullmatch(raw) is None:
        raise ValidationError(
            f"{field} must be a non-empty owner id containing only letters, "
            "numbers, '.', '_', or '-'"
        )
    return raw


def _source_context_suffix(source_context: str | None) -> str:
    return "" if source_context is None else f" against {source_context}"


def _portable_target_key(target: str) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(target).parts
    )


def _validate_portable_target_spellings(targets: list[str]) -> None:
    spellings: dict[tuple[str, ...], str] = {}
    for target in targets:
        key = _portable_target_key(target)
        previous = spellings.get(key)
        if previous is not None and previous != target:
            raise ValidationError(
                f"portable target spellings conflict: {previous} and {target}"
            )
        spellings[key] = target


def _validate_non_overlapping_targets(targets: set[str]) -> None:
    _validate_portable_target_spellings(list(targets))
    ordered = sorted(
        ((_portable_target_key(target), target) for target in targets),
        key=lambda item: (item[0], item[1]),
    )
    for (parent_key, parent), (child_key, child) in zip(ordered, ordered[1:]):
        if (
            len(parent_key) < len(child_key)
            and child_key[: len(parent_key)] == parent_key
        ):
            raise ValidationError(
                f"manifest targets must not overlap: {parent} and {child}"
            )


def _validate_target_hierarchy_changes(
    removed_targets: set[str],
    added_targets: set[str],
) -> None:
    # Each side already comes from a non-overlapping manifest. Therefore any
    # cross-version ancestor pair must be adjacent in the combined portable
    # sort order; a same-side path cannot sit between an ancestor and its
    # cross-side descendant without violating that manifest invariant.
    ordered = sorted(
        chain(
            (
                (_portable_target_key(target), "removed", target)
                for target in removed_targets
            ),
            (
                (_portable_target_key(target), "added", target)
                for target in added_targets
            ),
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    for left, right in zip(ordered, ordered[1:]):
        left_key, left_side, left_target = left
        right_key, right_side, right_target = right
        if left_side == right_side:
            continue
        if (
            len(left_key) < len(right_key)
            and right_key[: len(left_key)] == left_key
        ) or (
            len(right_key) < len(left_key)
            and left_key[: len(right_key)] == right_key
        ):
            previous_target = (
                left_target if left_side == "removed" else right_target
            )
            current_target = (
                left_target if left_side == "added" else right_target
            )
            raise ValidationError(
                "managed target hierarchy changes are not supported: "
                f"{previous_target} -> {current_target}"
            )


def _validate_historical_target_hierarchy(
    active_targets: set[str],
    historical_targets: set[str],
) -> None:
    historical_by_key = {
        _portable_target_key(target): target
        for target in historical_targets
    }
    ordered_historical_keys = sorted(historical_by_key)
    for active_target in active_targets:
        active_key = _portable_target_key(active_target)
        for prefix_length in range(1, len(active_key)):
            historical_target = historical_by_key.get(
                active_key[:prefix_length]
            )
            if historical_target is not None:
                raise ValidationError(
                    "managed target hierarchy changes are not supported: "
                    f"{historical_target} -> {active_target}"
                )
        descendant_index = bisect_right(ordered_historical_keys, active_key)
        if descendant_index >= len(ordered_historical_keys):
            continue
        descendant_key = ordered_historical_keys[descendant_index]
        if (
            len(descendant_key) > len(active_key)
            and descendant_key[: len(active_key)] == active_key
        ):
            historical_target = historical_by_key[descendant_key]
            raise ValidationError(
                "managed target hierarchy changes are not supported: "
                f"{historical_target} -> {active_target}"
            )


def _validate_transition_capacity(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    release_sha: str | None = None,
) -> None:
    removed_links = [
        previous_link
        for target, previous_link in previous["links"].items()
        if current["links"].get(target) != previous_link
    ]
    added_targets = set(current["links"]) - set(previous["links"])
    required_records = len(removed_links) + len(added_targets) + 1
    if required_records > MAX_PENDING_LINK_RECORDS:
        release_context = (
            "" if release_sha is None else f" from release {release_sha}"
        )
        raise ValidationError(
            "manifest link transition exceeds runtime transaction limit"
            f"{release_context}: {required_records} > {MAX_PENDING_LINK_RECORDS}"
        )
    runtime = _sync_runtime_module()
    try:
        runtime._validate_manifest_transition_capacity(
            _transition_capacity_profile(previous),
            _transition_capacity_profile(current),
        )
    except runtime.SyncError as error:
        release_context = (
            "" if release_sha is None else f" from release {release_sha}"
        )
        raise ValidationError(
            "manifest link transition exceeds runtime transaction byte capacity"
            f"{release_context}: {error}"
        ) from error


def _validate_replacement_retirement_graph(
    graph: dict[str, tuple[str, ...]],
) -> None:
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        cycle_keys = error.args[1] if len(error.args) > 1 else ()
        cycle = " -> ".join(str(key) for key in cycle_keys)
        detail = f": {cycle}" if cycle else ""
        raise ValidationError(
            f"replacement retirement cycle detected{detail}"
        ) from None


def _manifest_model(
    data: dict[str, Any],
    path_kind: ManifestPathKind | None = None,
    *,
    source_context: str | None = None,
    enforce_history_constraints: bool = True,
) -> dict[str, Any]:
    _validate_manifest_unicode_scalars(data)
    if data.get("version") != 1:
        raise ValidationError("sync manifest version must be 1")
    owner = _owner_id(
        data["owner"] if "owner" in data else PUBLIC_OWNER,
        "manifest owner",
    )
    context_suffix = _source_context_suffix(source_context)

    links: dict[str, dict[str, Any]] = {}
    raw_links = data.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise ValidationError("manifest links must be a non-empty array")
    if len(raw_links) > MAX_MANIFEST_ACTIVE_LINKS:
        raise ValidationError(
            "manifest active links exceed runtime transaction limit: "
            f"{len(raw_links)} > {MAX_MANIFEST_ACTIVE_LINKS}"
        )
    for raw_link in raw_links:
        if not isinstance(raw_link, dict):
            raise ValidationError("manifest link entries must be objects")
        source = _relative_path(raw_link.get("source"), "link source")
        target = _target_path(raw_link.get("target"), "link target")
        kind = raw_link.get("kind")
        if kind not in KINDS:
            raise ValidationError(f"link {target} has unsupported kind: {kind}")
        link_owner = _owner_id(
            raw_link["owner"] if "owner" in raw_link else owner,
            "link owner",
        )
        if link_owner != owner:
            raise ValidationError(
                f"manifest link {source} owner {link_owner} does not match "
                f"manifest owner {owner}"
            )
        override = raw_link.get("override", False)
        if not isinstance(override, bool):
            raise ValidationError(f"manifest link {source} override must be boolean")
        if owner == PUBLIC_OWNER and override:
            raise ValidationError(
                "public manifest links must not declare override=true"
            )
        if path_kind is not None:
            source_type = path_kind(source)
            if kind == "file":
                if source_type != "file":
                    raise ValidationError(
                        f"manifest file source is missing{context_suffix}: {source}"
                    )
            else:
                if source_type != "directory":
                    raise ValidationError(
                        "manifest directory source is missing"
                        f"{context_suffix}: {source}"
                    )
                if kind == "skill" and path_kind(f"{source}/SKILL.md") != "file":
                    raise ValidationError(
                        "manifest skill source is missing SKILL.md"
                        f"{context_suffix}: {source}"
                    )
        if target in links:
            raise ValidationError(f"duplicate manifest target: {target}")
        links[target] = {"source": source, "target": target, "kind": kind}
    _validate_non_overlapping_targets(set(links))

    raw_references = data.get("reference_only", [])
    if not isinstance(raw_references, list):
        raise ValidationError("reference_only must be an array when present")
    for raw_reference in raw_references:
        reference = _relative_path(raw_reference, "reference_only")
        if path_kind is not None and path_kind(reference) not in {
            "file",
            "directory",
        }:
            raise ValidationError(
                f"reference_only path is missing{context_suffix}: {reference}"
            )

    raw_base_release = data.get("base_release", {})
    if raw_base_release is None:
        raw_base_release = {}
    if not isinstance(raw_base_release, dict):
        raise ValidationError("base_release must be an object when present")
    base_release_repo = raw_base_release.get("repo")
    if base_release_repo is not None and (
        not isinstance(base_release_repo, str)
        or REPOSITORY_RE.fullmatch(base_release_repo) is None
    ):
        raise ValidationError(
            "base_release.repo must be an owner/repo string"
        )
    base_release_sha = raw_base_release.get("sha")
    if base_release_sha is not None and (
        not isinstance(base_release_sha, str)
        or FULL_SHA_RE.fullmatch(base_release_sha) is None
    ):
        raise ValidationError(
            "base_release.sha must be a 40-character lowercase hex SHA"
        )

    removed: dict[str, dict[str, Any]] = {}
    raw_removed = data.get("removed_links", [])
    if not isinstance(raw_removed, list):
        raise ValidationError("removed_links must be an array")
    for raw_entry in raw_removed:
        if not isinstance(raw_entry, dict):
            raise ValidationError("removed_links entries must be objects")
        unknown_fields = sorted(set(raw_entry) - REMOVED_LINK_FIELDS)
        if unknown_fields:
            raise ValidationError(
                "removed_links entry has unsupported field(s): "
                + ", ".join(unknown_fields)
            )
        removed_id = raw_entry.get("id")
        if not isinstance(removed_id, str) or ID_RE.fullmatch(removed_id) is None:
            raise ValidationError("removed link id has unsupported characters")
        if removed_id in removed:
            raise ValidationError(f"duplicate removed link id: {removed_id}")
        source = _relative_path(raw_entry.get("source"), "removed link source")
        target = _target_path(raw_entry.get("target"), "removed link target")
        kind = raw_entry.get("kind")
        if kind not in KINDS:
            raise ValidationError(f"removed link {removed_id} has unsupported kind: {kind}")
        replacement = raw_entry.get("replacement_target")
        if replacement is not None:
            replacement = _target_path(replacement, "replacement_target")
        raw_retires = raw_entry.get("retires_replacements", [])
        if not isinstance(raw_retires, list):
            raise ValidationError(
                f"removed link {removed_id} retires_replacements must be an array"
            )
        retires_replacements = tuple(raw_retires)
        if any(
            not isinstance(key, str) or REMOVED_KEY_RE.fullmatch(key) is None
            for key in retires_replacements
        ):
            raise ValidationError(
                f"removed link {removed_id} retires_replacements must use owner:id keys"
            )
        if len(set(retires_replacements)) != len(retires_replacements):
            raise ValidationError(
                f"removed link {removed_id} has duplicate retires_replacements entries"
            )
        legacy = raw_entry.get("legacy", False)
        if not isinstance(legacy, bool):
            raise ValidationError(f"removed link {removed_id} legacy must be boolean")
        removed[removed_id] = {
            "id": removed_id,
            "source": source,
            "target": target,
            "kind": kind,
            "replacement_target": replacement,
            "retires_replacements": retires_replacements,
            "legacy": legacy,
        }
    portable_targets = list(links)
    for entry in removed.values():
        portable_targets.append(entry["target"])
        if entry["replacement_target"] is not None:
            portable_targets.append(entry["replacement_target"])
    _validate_portable_target_spellings(portable_targets)
    for removed_id, entry in removed.items():
        entry_key = f"{owner}:{removed_id}"
        for retired_key in entry["retires_replacements"]:
            retired_owner, retired_id = retired_key.split(":", 1)
            if retired_key == entry_key:
                raise ValidationError(f"removed link {removed_id} cannot retire itself")
            if retired_owner != owner:
                continue
            retired = removed.get(retired_id)
            if retired is None:
                raise ValidationError(
                    f"removed link {removed_id} retires unknown replacement {retired_key}"
                )
            if retired["replacement_target"] != entry["target"]:
                raise ValidationError(
                    f"removed link {removed_id} target does not match replacement "
                    f"for {retired_key}"
                )
    removed_keys = {f"{owner}:{removed_id}" for removed_id in removed}
    retirement_graph = {
        f"{owner}:{removed_id}": tuple(
            retired_key
            for retired_key in entry["retires_replacements"]
            if retired_key in removed_keys
        )
        for removed_id, entry in removed.items()
    }
    _validate_replacement_retirement_graph(retirement_graph)
    if owner == PUBLIC_OWNER:
        retired_replacement_keys = {
            retired_key
            for entry in removed.values()
            for retired_key in entry["retires_replacements"]
        }
        for removed_id, entry in removed.items():
            replacement = entry["replacement_target"]
            removed_key = f"{owner}:{removed_id}"
            if (
                replacement is not None
                and replacement not in links
                and removed_key not in retired_replacement_keys
            ):
                raise ValidationError(
                    f"replacement target {replacement} is unavailable for active "
                    f"removal {removed_key}"
                )
    if enforce_history_constraints:
        historical_targets = {
            entry["target"]
            for entry in removed.values()
        }
        _validate_historical_target_hierarchy(set(links), historical_targets)
        declared_transition_targets = set(links) | historical_targets
        required_historical_records = len(declared_transition_targets) + 1
        if required_historical_records > MAX_PENDING_LINK_RECORDS:
            raise ValidationError(
                "manifest declared history exceeds runtime transaction limit: "
                f"{required_historical_records} > {MAX_PENDING_LINK_RECORDS}"
            )
    return {"owner": owner, "links": links, "removed": removed}


def _validate_removed_link_history_preserved(
    historical_removed: dict[str, dict[str, Any]],
    current_removed: dict[str, dict[str, Any]],
    *,
    release_sha: str | None = None,
) -> None:
    for removed_id, historical_entry in historical_removed.items():
        if current_removed.get(removed_id) == historical_entry:
            continue
        release_context = (
            "" if release_sha is None else f" from release {release_sha}"
        )
        raise ValidationError(
            "removed link history changed or disappeared"
            f"{release_context}: {removed_id}"
        )


def validate_manifest_change(
    previous_data: dict[str, Any],
    current_data: dict[str, Any],
    *,
    known_prior_removed: dict[str, dict[str, Any]] | None = None,
    previous_path_kind: ManifestPathKind | None = None,
    current_path_kind: ManifestPathKind | None = None,
    previous_source_context: str | None = None,
    current_source_context: str | None = None,
) -> None:
    previous = _manifest_model(
        previous_data,
        previous_path_kind,
        source_context=previous_source_context,
        enforce_history_constraints=False,
    )
    current = _manifest_model(
        current_data,
        current_path_kind,
        source_context=current_source_context,
    )
    if previous["owner"] != current["owner"]:
        raise ValidationError("manifest owner must not change between releases")

    previous_removed = previous["removed"]
    current_removed = current["removed"]
    prior_removed = dict(previous_removed)
    if known_prior_removed is not None:
        for removed_id, historical_entry in known_prior_removed.items():
            previous_entry = prior_removed.get(removed_id)
            if previous_entry is not None and previous_entry != historical_entry:
                raise ValidationError(
                    "known prior removed link conflicts with previous manifest: "
                    f"{removed_id}"
                )
            prior_removed[removed_id] = historical_entry
    _validate_removed_link_history_preserved(prior_removed, current_removed)

    removed_links = [
        previous_link
        for target, previous_link in previous["links"].items()
        if current["links"].get(target) != previous_link
    ]
    removed_targets = set(previous["links"]) - set(current["links"])
    added_targets = set(current["links"]) - set(previous["links"])
    _validate_transition_capacity(previous, current)
    _validate_target_hierarchy_changes(removed_targets, added_targets)
    new_removed = {
        removed_id: entry
        for removed_id, entry in current_removed.items()
        if removed_id not in prior_removed
    }
    new_removed_by_identity: dict[
        tuple[str, str, str],
        list[dict[str, Any]],
    ] = {}
    for entry in new_removed.values():
        identity = (entry["source"], entry["target"], entry["kind"])
        new_removed_by_identity.setdefault(identity, []).append(entry)
    previous_replacements_by_target: dict[str, set[str]] = {}
    for removed_id, entry in prior_removed.items():
        replacement_target = entry["replacement_target"]
        if replacement_target is None:
            continue
        previous_replacements_by_target.setdefault(
            replacement_target,
            set(),
        ).add(f"{previous['owner']}:{removed_id}")
    matched_new_ids: set[str] = set()
    for previous_link in sorted(
        removed_links,
        key=lambda entry: (entry["target"], entry["source"], entry["kind"]),
    ):
        matches = new_removed_by_identity.get(
            (
                previous_link["source"],
                previous_link["target"],
                previous_link["kind"],
            ),
            [],
        )
        if len(matches) != 1:
            raise ValidationError(
                "removed link requires one new matching removed_links entry: "
                f"{previous_link['source']} -> {previous_link['target']} "
                f"({previous_link['kind']})"
            )
        matching_removal = matches[0]
        required_retirements: set[str] = set()
        if previous_link["target"] not in current["links"]:
            required_retirements = previous_replacements_by_target.get(
                previous_link["target"],
                set(),
            )
        missing_retirements = required_retirements - set(
            matching_removal["retires_replacements"]
        )
        if missing_retirements:
            raise ValidationError(
                f"removed link {matching_removal['id']} must retire historical "
                f"replacements: {', '.join(sorted(missing_retirements))}"
            )
        matched_new_ids.add(matching_removal["id"])

    for removed_id, entry in current_removed.items():
        if removed_id in prior_removed or removed_id in matched_new_ids:
            continue
        if not entry["legacy"]:
            raise ValidationError(
                f"unexplained removed link must declare legacy=true: {removed_id}"
            )


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
                raise ValidationError(
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
                raise ValidationError(
                    "serialized release manifest exceeds "
                    f"{MAX_RELEASE_MANIFEST_BYTES} bytes"
                )
            output.write(chunk.encode("ascii", errors="strict"))
        if output.tell() >= MAX_RELEASE_MANIFEST_BYTES:
            raise ValidationError(
                "serialized release manifest exceeds "
                f"{MAX_RELEASE_MANIFEST_BYTES} bytes"
            )
        output.write(b"\n")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ValidationError(
            f"failed to serialize release manifest: {error}"
        ) from error
    return output.getvalue()


def _parse_manifest_bytes(payload: bytes, description: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError(f"{description} is not valid UTF-8: {error}") from error
    try:
        data = json.loads(text, parse_int=_bounded_json_integer)
    except (ValueError, RecursionError) as error:
        raise ValidationError(f"{description} is invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValidationError(f"{description} must be a JSON object")
    _release_manifest_payload(data)
    return data


def _inode_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def _live_worktree_root_identity(repo_root: Path) -> tuple[int, int, int]:
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    root_fd: int | None = None
    try:
        root_fd = os.open(repo_root, directory_flags)
        opened = os.fstat(root_fd)
        named = os.stat(repo_root, follow_symlinks=False)
        identity = _inode_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _inode_identity(named) != identity
        ):
            raise ValidationError("live working tree root is not a stable directory")
        return identity
    except ValidationError:
        raise
    except (OSError, ValueError) as error:
        raise ValidationError(
            f"failed to bind live working tree root safely: {error}"
        ) from error
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _live_worktree_path_kind_resolver(
    repo_root: Path,
    expected_root_identity: tuple[int, int, int] | None = None,
) -> _LiveWorktreePathKind:
    if expected_root_identity is None:
        expected_root_identity = _live_worktree_root_identity(repo_root)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    captured_paths: dict[
        str,
        tuple[
            str | None,
            tuple[tuple[str, tuple[int, int, int] | None], ...],
        ],
    ] = {}

    def path_kind(raw_path: str) -> str | None:
        path = _relative_path(raw_path, "manifest source")
        encoded_path = path.encode("utf-8", errors="strict")
        parts = PurePosixPath(path).parts
        if len(encoded_path) > MAX_MANIFEST_PATH_BYTES:
            raise ValidationError(
                "manifest source path exceeds the 4096-byte live traversal limit: "
                f"{path}"
            )
        if len(parts) > MAX_MANIFEST_SOURCE_DEPTH:
            raise ValidationError(
                "manifest source path exceeds the 64-component live traversal "
                f"limit: {path}"
            )

        open_directories: list[int] = []
        bindings: list[tuple[int, str, int, tuple[int, int, int]]] = []
        snapshot_entries: list[
            tuple[str, tuple[int, int, int] | None]
        ] = []
        file_fd: int | None = None
        file_binding: tuple[int, str, int, tuple[int, int, int]] | None = None
        try:
            root_fd = os.open(repo_root, directory_flags)
            open_directories.append(root_fd)
            root_metadata = os.fstat(root_fd)
            root_named = os.stat(repo_root, follow_symlinks=False)
            root_identity = _inode_identity(root_metadata)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or _inode_identity(root_named) != root_identity
                or root_identity != expected_root_identity
            ):
                raise ValidationError(
                    "live working tree root is not a stable directory"
                )
            snapshot_entries.append((".", root_identity))

            parent_fd = root_fd
            missing = False
            for part_index, part in enumerate(parts[:-1]):
                relative_part = PurePosixPath(*parts[: part_index + 1]).as_posix()
                try:
                    named_metadata = os.stat(
                        part,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except (FileNotFoundError, NotADirectoryError):
                    snapshot_entries.append((relative_part, None))
                    missing = True
                    break
                if stat.S_ISLNK(named_metadata.st_mode):
                    raise ValidationError(
                        "manifest source path contains a symlink component in the "
                        f"live working tree: {path}"
                    )
                if not stat.S_ISDIR(named_metadata.st_mode):
                    snapshot_entries.append(
                        (relative_part, _inode_identity(named_metadata))
                    )
                    missing = True
                    break
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                open_directories.append(child_fd)
                child_metadata = os.fstat(child_fd)
                identity = _inode_identity(child_metadata)
                if (
                    not stat.S_ISDIR(child_metadata.st_mode)
                    or _inode_identity(named_metadata) != identity
                ):
                    raise ValidationError(
                        "manifest source path changed during live traversal: "
                        f"{path}"
                    )
                bindings.append((parent_fd, part, child_fd, identity))
                snapshot_entries.append((relative_part, identity))
                parent_fd = child_fd

            kind: str | None = None
            if not missing:
                leaf = parts[-1]
                try:
                    leaf_named = os.stat(
                        leaf,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except (FileNotFoundError, NotADirectoryError):
                    leaf_named = None
                if leaf_named is not None:
                    if stat.S_ISLNK(leaf_named.st_mode):
                        raise ValidationError(
                            "manifest source path is a symlink in the live working "
                            f"tree: {path}"
                        )
                    leaf_identity = _inode_identity(leaf_named)
                    if stat.S_ISREG(leaf_named.st_mode):
                        file_fd = os.open(leaf, file_flags, dir_fd=parent_fd)
                        opened_metadata = os.fstat(file_fd)
                        if (
                            not stat.S_ISREG(opened_metadata.st_mode)
                            or _inode_identity(opened_metadata) != leaf_identity
                        ):
                            raise ValidationError(
                                "manifest source path changed during live traversal: "
                                f"{path}"
                            )
                        file_binding = (
                            parent_fd,
                            leaf,
                            file_fd,
                            leaf_identity,
                        )
                        snapshot_entries.append((path, leaf_identity))
                        kind = "file"
                    elif stat.S_ISDIR(leaf_named.st_mode):
                        leaf_fd = os.open(leaf, directory_flags, dir_fd=parent_fd)
                        open_directories.append(leaf_fd)
                        opened_metadata = os.fstat(leaf_fd)
                        if (
                            not stat.S_ISDIR(opened_metadata.st_mode)
                            or _inode_identity(opened_metadata) != leaf_identity
                        ):
                            raise ValidationError(
                                "manifest source path changed during live traversal: "
                                f"{path}"
                            )
                        bindings.append((parent_fd, leaf, leaf_fd, leaf_identity))
                        snapshot_entries.append((path, leaf_identity))
                        kind = "directory"
                    else:
                        raise ValidationError(
                            "manifest source path has an unsafe kind in the live "
                            f"working tree: {path}"
                        )
                else:
                    snapshot_entries.append((path, None))

            if file_binding is not None:
                bound_parent, name, bound_fd, identity = file_binding
                current_named = os.stat(
                    name,
                    dir_fd=bound_parent,
                    follow_symlinks=False,
                )
                if (
                    _inode_identity(os.fstat(bound_fd)) != identity
                    or _inode_identity(current_named) != identity
                ):
                    raise ValidationError(
                        "manifest source path changed during live traversal: "
                        f"{path}"
                    )
            for bound_parent, name, bound_fd, identity in reversed(bindings):
                current_named = os.stat(
                    name,
                    dir_fd=bound_parent,
                    follow_symlinks=False,
                )
                if (
                    _inode_identity(os.fstat(bound_fd)) != identity
                    or _inode_identity(current_named) != identity
                ):
                    raise ValidationError(
                        "manifest source path changed during live traversal: "
                        f"{path}"
                    )
            if (
                _inode_identity(os.fstat(root_fd)) != root_identity
                or _inode_identity(os.stat(repo_root, follow_symlinks=False))
                != root_identity
            ):
                raise ValidationError(
                    "live working tree root changed during source traversal"
                )
            captured = (kind, tuple(snapshot_entries))
            if path in captured_paths and captured_paths[path] != captured:
                raise ValidationError(
                    "manifest source path changed between live validation passes: "
                    f"{path}"
                )
            captured_paths[path] = captured
            return kind
        except ValidationError:
            raise
        except (OSError, ValueError) as error:
            raise ValidationError(
                f"failed to inspect live manifest source safely {path}: {error}"
            ) from error
        finally:
            if file_fd is not None:
                os.close(file_fd)
            for directory_fd in reversed(open_directories):
                os.close(directory_fd)

    def verify() -> None:
        for path in tuple(sorted(captured_paths)):
            path_kind(path)

    return _LiveWorktreePathKind(path_kind, verify)


def _regular_file_snapshot(
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


def _read_repo_manifest_snapshot(
    repo_root: Path,
    manifest: Path,
    expected_root_identity: tuple[int, int, int] | None = None,
) -> _LiveManifestSnapshot:
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    open_directories: list[int] = []
    bindings: list[tuple[int, str, int, tuple[int, int, int]]] = []
    ancestor_identities: list[tuple[str, tuple[int, int, int]]] = []
    leaf_fd: int | None = None
    try:
        root_fd = os.open(repo_root, directory_flags)
        open_directories.append(root_fd)
        root_before = os.fstat(root_fd)
        root_named_before = os.stat(repo_root, follow_symlinks=False)
        root_identity = _inode_identity(root_before)
        if (
            not stat.S_ISDIR(root_before.st_mode)
            or root_identity != _inode_identity(root_named_before)
            or (
                expected_root_identity is not None
                and root_identity != expected_root_identity
            )
        ):
            raise ValidationError("repository root identity is not stable")

        parent_fd = root_fd
        for part_index, part in enumerate(manifest.parts[:-1]):
            child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            open_directories.append(child_fd)
            child_metadata = os.fstat(child_fd)
            named_metadata = os.stat(
                part,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            identity = _inode_identity(child_metadata)
            if (
                not stat.S_ISDIR(child_metadata.st_mode)
                or identity != _inode_identity(named_metadata)
            ):
                raise ValidationError(
                    f"manifest ancestor is not a stable directory: {part}"
                )
            bindings.append((parent_fd, part, child_fd, identity))
            ancestor_identities.append(
                (
                    PurePosixPath(*manifest.parts[: part_index + 1]).as_posix(),
                    identity,
                )
            )
            parent_fd = child_fd

        leaf_name = manifest.parts[-1]
        leaf_fd = os.open(leaf_name, file_flags, dir_fd=parent_fd)
        leaf_before = os.fstat(leaf_fd)
        leaf_named_before = os.stat(
            leaf_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(leaf_before.st_mode):
            raise ValidationError(f"manifest is not a regular file: {manifest}")
        if _inode_identity(leaf_before) != _inode_identity(leaf_named_before):
            raise ValidationError(f"manifest identity is not stable: {manifest}")
        if leaf_before.st_size > MAX_RELEASE_MANIFEST_BYTES:
            raise ValidationError(
                f"manifest {manifest} exceeds {MAX_RELEASE_MANIFEST_BYTES} bytes"
            )
        leaf_snapshot = _regular_file_snapshot(leaf_before)

        payload = bytearray()
        while len(payload) <= MAX_RELEASE_MANIFEST_BYTES:
            remaining = MAX_RELEASE_MANIFEST_BYTES + 1 - len(payload)
            chunk = os.read(leaf_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            payload.extend(chunk)

        leaf_after = os.fstat(leaf_fd)
        leaf_named_after = os.stat(
            leaf_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _regular_file_snapshot(leaf_after) != leaf_snapshot
            or _inode_identity(leaf_named_after) != _inode_identity(leaf_after)
        ):
            raise ValidationError(f"manifest changed while being read: {manifest}")

        for bound_parent, part, child_fd, identity in reversed(bindings):
            child_after = os.fstat(child_fd)
            named_after = os.stat(
                part,
                dir_fd=bound_parent,
                follow_symlinks=False,
            )
            if (
                _inode_identity(child_after) != identity
                or _inode_identity(named_after) != identity
            ):
                raise ValidationError(
                    f"manifest ancestor changed while being read: {part}"
                )

        root_after = os.fstat(root_fd)
        root_named_after = os.stat(repo_root, follow_symlinks=False)
        if (
            _inode_identity(root_after) != _inode_identity(root_before)
            or _inode_identity(root_named_after) != _inode_identity(root_before)
        ):
            raise ValidationError("repository root changed while reading manifest")
    except ValidationError:
        raise
    except (OSError, ValueError) as error:
        raise ValidationError(
            f"failed to read manifest safely {manifest}: {error}"
        ) from error
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        for directory_fd in reversed(open_directories):
            os.close(directory_fd)

    if len(payload) > MAX_RELEASE_MANIFEST_BYTES:
        raise ValidationError(
            f"manifest {manifest} exceeds {MAX_RELEASE_MANIFEST_BYTES} bytes"
        )
    return _LiveManifestSnapshot(
        payload=bytes(payload),
        root_identity=root_identity,
        ancestor_identities=tuple(ancestor_identities),
        file_snapshot=leaf_snapshot,
    )


def _read_repo_manifest_bytes(
    repo_root: Path,
    manifest: Path,
    expected_root_identity: tuple[int, int, int] | None = None,
) -> bytes:
    return _read_repo_manifest_snapshot(
        repo_root,
        manifest,
        expected_root_identity,
    ).payload


def _load_json(
    repo_root: Path,
    manifest: Path,
    expected_root_identity: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    _manifest_git_path(manifest)
    payload = _read_repo_manifest_bytes(
        repo_root,
        manifest,
        expected_root_identity,
    )
    return _parse_manifest_bytes(payload, f"manifest {manifest}")


def _verify_live_worktree_snapshot(
    repo_root: Path,
    manifest: Path,
    expected_manifest: _LiveManifestSnapshot,
    path_kind: _LiveWorktreePathKind,
) -> None:
    def verify_manifest() -> None:
        actual_manifest = _read_repo_manifest_snapshot(
            repo_root,
            manifest,
            expected_manifest.root_identity,
        )
        if actual_manifest != expected_manifest:
            raise ValidationError(
                "live manifest changed while validating its source paths"
            )

    verify_manifest()
    path_kind.verify()
    verify_manifest()


def _manifest_git_path(manifest: Path) -> tuple[str, bytes]:
    raw = manifest.as_posix()
    if (
        not raw
        or raw == "."
        or manifest.is_absolute()
        or any(part in {"", ".", ".."} for part in manifest.parts)
    ):
        raise ValidationError(f"manifest must be a safe relative path: {raw}")
    if "\0" in raw:
        raise ValidationError("manifest path must not contain embedded NUL")
    if "\n" in raw or "\r" in raw:
        raise ValidationError("manifest path must not contain line breaks")
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValidationError("manifest path must be valid UTF-8") from error
    if len(encoded) > MAX_MANIFEST_PATH_BYTES:
        raise ValidationError(
            f"manifest path exceeds {MAX_MANIFEST_PATH_BYTES} UTF-8 bytes"
        )
    return raw, encoded


def _resolve_commit(repo_root: Path, ref: str) -> str:
    if not ref or "\0" in ref:
        raise ValidationError("base ref must be a non-empty value without NUL")
    try:
        ref.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValidationError("base ref must be valid UTF-8") from error
    result = _bounded_git_output(
        repo_root,
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        stdout_limit=MAX_GIT_METADATA_BYTES,
        stdout_overflow_error="Git commit identity output exceeds the safety limit",
    )
    if result.returncode != 0:
        raise ValidationError(
            f"failed to resolve base ref {ref}: {_git_error(result)}"
        )
    commit = result.stdout.strip()
    if GIT_OBJECT_ID_RE.fullmatch(commit) is None:
        raise ValidationError(f"base ref {ref} returned an invalid commit identity")
    return commit.decode("ascii")


def _git_tree_path_kind_resolver(
    repo_root: Path,
    commit: str,
) -> ManifestPathKind:
    try:
        encoded_commit = commit.encode("ascii", errors="strict")
    except UnicodeEncodeError as error:
        raise ValidationError(
            "Git tree path-kind resolver requires a resolved commit identity"
        ) from error
    if GIT_OBJECT_ID_RE.fullmatch(encoded_commit) is None:
        raise ValidationError(
            "Git tree path-kind resolver requires a resolved commit identity"
        )
    result = _bounded_git_output(
        repo_root,
        [
            "git",
            "ls-tree",
            "-r",
            "-t",
            "-z",
            "--full-tree",
            commit,
        ],
        stdout_limit=MAX_GIT_TREE_LISTING_BYTES,
        stdout_overflow_error=(
            "Git tree path-kind listing exceeds the 16 MiB safety limit"
        ),
    )
    if result.returncode != 0:
        raise ValidationError(
            f"failed to inspect Git tree at {commit}: {_git_error(result)}"
        )
    if result.stdout and not result.stdout.endswith(b"\0"):
        raise ValidationError("Git returned a malformed tree path-kind listing")

    path_kinds: dict[bytes, str | None] = {}
    records = result.stdout[:-1].split(b"\0") if result.stdout else []
    for record in records:
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3 or not raw_path:
            raise ValidationError("Git returned a malformed tree path-kind entry")
        mode, object_type, object_id = fields
        if GIT_OBJECT_ID_RE.fullmatch(object_id) is None:
            raise ValidationError(
                "Git returned an invalid tree path-kind object identity"
            )
        if mode == b"040000" and object_type == b"tree":
            kind: str | None = "directory"
        elif mode in REGULAR_GIT_MODES and object_type == b"blob":
            kind = "file"
        elif (
            mode == b"120000" and object_type == b"blob"
        ) or (
            mode == b"160000" and object_type == b"commit"
        ):
            kind = None
        else:
            raise ValidationError(
                "Git returned an unsupported tree path-kind entry"
            )
        if raw_path in path_kinds:
            raise ValidationError(
                "Git returned a duplicate tree path-kind entry"
            )
        path_kinds[raw_path] = kind

    def path_kind(path: str) -> str | None:
        try:
            encoded_path = path.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ValidationError(
                "manifest source path must be valid UTF-8"
            ) from error
        return path_kinds.get(encoded_path)

    return path_kind


def _manifest_blob_at_commit(
    repo_root: Path,
    commit: str,
    manifest: Path,
) -> str | None:
    raw_path, encoded_path = _manifest_git_path(manifest)
    env = os.environ.copy()
    env["GIT_LITERAL_PATHSPECS"] = "1"
    result = _bounded_git_output(
        repo_root,
        ["git", "ls-tree", "--full-tree", "-z", commit, "--", raw_path],
        env=env,
        stdout_limit=MAX_GIT_METADATA_BYTES,
        stdout_overflow_error="Git manifest identity output exceeds the safety limit",
    )
    if result.returncode != 0:
        raise ValidationError(
            f"failed to inspect base manifest at {commit}: {_git_error(result)}"
        )
    if not result.stdout:
        return None
    if not result.stdout.endswith(b"\0"):
        raise ValidationError("Git returned a malformed manifest identity")
    records = result.stdout[:-1].split(b"\0")
    if len(records) != 1:
        raise ValidationError("Git returned multiple manifest identities")
    metadata, separator, returned_path = records[0].partition(b"\t")
    fields = metadata.split()
    if separator != b"\t" or len(fields) != 3 or returned_path != encoded_path:
        raise ValidationError("Git returned a mismatched manifest identity")
    mode, object_type, object_id = fields
    if mode not in REGULAR_GIT_MODES or object_type != b"blob":
        raise ValidationError(
            f"base manifest at {commit} is not a regular file"
        )
    if GIT_OBJECT_ID_RE.fullmatch(object_id) is None:
        raise ValidationError("Git returned an invalid manifest object identity")
    return object_id.decode("ascii")


def _read_git_manifest_blob(
    repo_root: Path,
    object_id: str,
    description: str,
) -> bytes:
    type_result = _bounded_git_output(
        repo_root,
        ["git", "cat-file", "-t", object_id],
        stdout_limit=MAX_GIT_METADATA_BYTES,
        stdout_overflow_error="Git object type output exceeds the safety limit",
    )
    if type_result.returncode != 0:
        raise ValidationError(
            f"failed to inspect {description} object type: {_git_error(type_result)}"
        )
    if type_result.stdout.strip() != b"blob":
        raise ValidationError(f"{description} is not a Git blob")

    size_result = _bounded_git_output(
        repo_root,
        ["git", "cat-file", "-s", object_id],
        stdout_limit=MAX_GIT_METADATA_BYTES,
        stdout_overflow_error="Git object size output exceeds the safety limit",
    )
    if size_result.returncode != 0:
        raise ValidationError(
            f"failed to inspect {description} size: {_git_error(size_result)}"
        )
    raw_size = size_result.stdout.strip()
    if not raw_size.isdigit():
        raise ValidationError(f"Git returned an invalid size for {description}")
    declared_size = int(raw_size)
    if declared_size > MAX_RELEASE_MANIFEST_BYTES:
        raise ValidationError(
            f"{description} exceeds {MAX_RELEASE_MANIFEST_BYTES} bytes"
        )

    blob_result = _bounded_git_output(
        repo_root,
        ["git", "cat-file", "blob", object_id],
        stdout_limit=declared_size,
        stdout_overflow_error=f"{description} exceeds its declared Git object size",
    )
    if blob_result.returncode != 0:
        raise ValidationError(
            f"failed to read {description}: {_git_error(blob_result)}"
        )
    if len(blob_result.stdout) != declared_size:
        raise ValidationError(
            f"{description} size changed while it was being read"
        )
    return blob_result.stdout


def _manifest_at_ref(repo_root: Path, ref: str, manifest: Path) -> dict[str, Any] | None:
    _manifest_git_path(manifest)
    commit = _resolve_commit(repo_root, ref)
    object_id = _manifest_blob_at_commit(repo_root, commit, manifest)
    if object_id is None:
        return None
    description = f"base manifest at {ref}"
    payload = _read_git_manifest_blob(repo_root, object_id, description)
    return _parse_manifest_bytes(payload, description)


def _release_manifests_at_commits(
    repo_root: Path,
    commits: list[str],
    manifest: Path,
) -> list[tuple[str, dict[str, Any]]]:
    manifest_path, _encoded_path = _manifest_git_path(manifest)
    queries = [f"{commit}:{manifest_path}" for commit in commits]
    query_payload = ("\n".join(queries) + "\n").encode("utf-8")
    metadata_result = _bounded_git_output(
        repo_root,
        [
            "git",
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        ],
        stdin_data=query_payload,
        stdout_limit=MAX_GIT_RELEASE_HISTORY_BYTES,
        stdout_overflow_error=(
            "Git release manifest metadata exceeds the 1 MiB safety limit"
        ),
    )
    if metadata_result.returncode != 0:
        raise ValidationError(
            "failed to inspect release manifest history: "
            f"{_git_error(metadata_result)}"
        )
    metadata_lines = metadata_result.stdout.splitlines()
    if len(metadata_lines) != len(commits):
        raise ValidationError(
            "Git release manifest metadata did not match the query count"
        )

    commit_objects: list[tuple[str, bytes]] = []
    object_sizes: dict[bytes, int] = {}
    for commit, query, line in zip(commits, queries, metadata_lines):
        fields = line.split()
        if line.endswith(b" missing"):
            raise ValidationError(
                f"complete release {commit} does not contain {manifest}"
            )
        if (
            len(fields) != 3
            or GIT_OBJECT_ID_RE.fullmatch(fields[0]) is None
            or fields[1] != b"blob"
            or not fields[2].isdigit()
        ):
            raise ValidationError(
                f"complete release {commit} returned invalid manifest metadata"
            )
        object_id = fields[0]
        object_size = int(fields[2])
        if object_size > MAX_RELEASE_MANIFEST_BYTES:
            raise ValidationError(
                f"complete release {commit} manifest exceeds "
                f"{MAX_RELEASE_MANIFEST_BYTES} bytes"
            )
        previous_size = object_sizes.setdefault(object_id, object_size)
        if previous_size != object_size:
            raise ValidationError(
                f"Git returned conflicting sizes for release manifest {query}"
            )
        commit_objects.append((commit, object_id))

    total_manifest_bytes = sum(object_sizes.values())
    if total_manifest_bytes > MAX_RELEASE_MANIFEST_HISTORY_BYTES:
        raise ValidationError(
            "release manifest history exceeds byte limit: "
            f"{total_manifest_bytes} > {MAX_RELEASE_MANIFEST_HISTORY_BYTES}"
        )
    object_ids = list(object_sizes)
    object_payload = b"\n".join(object_ids) + b"\n"
    content_result = _bounded_git_output(
        repo_root,
        [
            "git",
            "cat-file",
            "--batch=%(objectname) %(objecttype) %(objectsize)",
        ],
        stdin_data=object_payload,
        stdout_limit=(
            total_manifest_bytes + len(object_ids) * 96
        ),
        stdout_overflow_error=(
            "Git release manifest history exceeds its declared size"
        ),
    )
    if content_result.returncode != 0:
        raise ValidationError(
            "failed to read release manifest history: "
            f"{_git_error(content_result)}"
        )

    manifests_by_object: dict[bytes, dict[str, Any]] = {}
    cursor = 0
    for object_id in object_ids:
        header_end = content_result.stdout.find(b"\n", cursor)
        if header_end < 0:
            raise ValidationError("Git returned truncated release manifest metadata")
        header = content_result.stdout[cursor:header_end].split()
        expected_size = object_sizes[object_id]
        if (
            len(header) != 3
            or header[0] != object_id
            or header[1] != b"blob"
            or not header[2].isdigit()
            or int(header[2]) != expected_size
        ):
            raise ValidationError("Git returned mismatched release manifest metadata")
        payload_start = header_end + 1
        payload_end = payload_start + expected_size
        if (
            payload_end >= len(content_result.stdout)
            or content_result.stdout[payload_end : payload_end + 1] != b"\n"
        ):
            raise ValidationError("Git returned truncated release manifest content")
        payload = content_result.stdout[payload_start:payload_end]
        manifests_by_object[object_id] = _parse_manifest_bytes(
            payload,
            f"release manifest object {object_id.decode('ascii')}",
        )
        cursor = payload_end + 1
    if cursor != len(content_result.stdout):
        raise ValidationError("Git returned trailing release manifest content")

    return [
        (commit, manifests_by_object[object_id])
        for commit, object_id in commit_objects
    ]


def _github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValidationError("GITHUB_TOKEN is required with --release-repo")
    return token


def _request_json(url: str, token: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(MAX_GITHUB_API_RESPONSE_BYTES + 1)
    except (OSError, ValueError) as error:
        raise ValidationError(f"failed to query GitHub releases API: {error}") from error
    if len(body) > MAX_GITHUB_API_RESPONSE_BYTES:
        raise ValidationError(
            "GitHub releases API response exceeds byte limit: "
            f"> {MAX_GITHUB_API_RESPONSE_BYTES}"
        )
    if not body:
        raise ValidationError("GitHub releases API returned an empty response")
    try:
        return json.loads(
            body.decode("utf-8"),
            parse_int=_bounded_json_integer,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ValidationError(
            f"GitHub releases API returned invalid JSON: {error}"
        ) from error


def _iter_github_releases(repository: str, token: str):
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ValidationError("--release-repo must use the OWNER/REPOSITORY form")
    owner, name = repository.split("/", 1)
    repository_path = f"{quote(owner, safe='')}/{quote(name, safe='')}"
    release_count = 0
    for page in range(1, MAX_GITHUB_RELEASE_PAGES + 1):
        releases = _request_json(
            f"{GITHUB_API_ROOT}/repos/{repository_path}/releases"
            f"?per_page={GITHUB_RELEASES_PAGE_SIZE}&page={page}",
            token,
        )
        if not isinstance(releases, list):
            raise ValidationError("GitHub releases API returned an unexpected payload")
        next_release_count = release_count + len(releases)
        if next_release_count > MAX_GITHUB_RELEASES:
            raise ValidationError(
                "GitHub releases API exceeds release limit: "
                f"{next_release_count} > {MAX_GITHUB_RELEASES}"
            )
        release_count = next_release_count
        for release in releases:
            if not isinstance(release, dict):
                raise ValidationError("GitHub releases API returned an invalid release")
            yield release
        if len(releases) < GITHUB_RELEASES_PAGE_SIZE:
            return
    raise ValidationError(
        "GitHub releases API exceeds pagination limit: "
        f"{MAX_GITHUB_RELEASE_PAGES} pages"
    )


def _complete_release_identity(
    release: dict[str, Any],
) -> tuple[str, str, str] | None:
    if release.get("draft") or release.get("prerelease"):
        return None
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None

    archive_shas: list[str] = []
    checksum_sha_counts: dict[str, int] = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("state") != "uploaded":
            continue
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        archive_match = RELEASE_ARCHIVE_ASSET_RE.fullmatch(name)
        if archive_match is not None:
            archive_shas.append(archive_match.group(1))
            continue
        checksum_match = RELEASE_CHECKSUM_ASSET_RE.fullmatch(name)
        if checksum_match is not None:
            checksum_sha = checksum_match.group(1)
            checksum_sha_counts[checksum_sha] = (
                checksum_sha_counts.get(checksum_sha, 0) + 1
            )

    complete_shas = {sha for sha in archive_shas if sha in checksum_sha_counts}
    if not complete_shas:
        return None
    if len(archive_shas) != 1:
        raise ValidationError(
            "complete published release has multiple personal-codex tarball assets"
        )
    sha = archive_shas[0]
    if checksum_sha_counts[sha] != 1:
        raise ValidationError(
            "complete published release has multiple matching checksum assets"
        )

    tag_name = release.get("tag_name")
    if not isinstance(tag_name, str):
        raise ValidationError("complete published release has no valid tag name")
    tag_match = RELEASE_TAG_RE.fullmatch(tag_name)
    if tag_match is None:
        raise ValidationError(
            f"complete published release has invalid tag name: {tag_name}"
        )
    if not sha.startswith(tag_match.group(1)):
        raise ValidationError(
            f"release asset SHA {sha} does not match tag suffix {tag_match.group(1)}"
        )

    target_commitish = release.get("target_commitish")
    if (
        isinstance(target_commitish, str)
        and FULL_SHA_RE.fullmatch(target_commitish) is not None
        and target_commitish != sha
    ):
        raise ValidationError(
            f"release asset SHA {sha} does not match target commit {target_commitish}"
        )

    published_at = release.get("published_at")
    if not isinstance(published_at, str) or PUBLISHED_AT_RE.fullmatch(published_at) is None:
        raise ValidationError(
            "complete published release has an invalid published_at timestamp"
        )
    return published_at, tag_name, sha


def _complete_release_identities(
    repository: str,
    token: str,
) -> list[tuple[str, str]]:
    identities: list[tuple[str, str]] = []
    for release in _iter_github_releases(repository, token):
        identity = _complete_release_identity(release)
        if identity is not None:
            _published_at, tag_name, sha = identity
            identities.append((tag_name, sha))
    if not identities:
        raise ValidationError(f"no complete published release found for {repository}")
    return identities


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = _bounded_git_output(
        repo_root,
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        stdout_limit=MAX_GIT_METADATA_BYTES,
        stdout_overflow_error="Git merge-base output exceeds the safety limit",
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise ValidationError(
        "failed to verify release history ancestry: "
        f"{_git_error(result)}"
    )


def _verified_release_shas(
    repo_root: Path,
    identities: list[tuple[str, str]],
) -> list[str]:
    tag_queries = [f"refs/tags/{tag_name}^{{commit}}" for tag_name, _sha in identities]
    result = _bounded_git_output(
        repo_root,
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        stdin_data=("\n".join(tag_queries) + "\n").encode("ascii"),
        stdout_limit=MAX_GIT_RELEASE_HISTORY_BYTES,
        stdout_overflow_error="Git release tag output exceeds the 1 MiB safety limit",
    )
    if result.returncode != 0:
        raise ValidationError(
            f"failed to resolve release tags: {_git_error(result)}"
        )
    lines = result.stdout.splitlines()
    if len(lines) != len(identities):
        raise ValidationError("Git release tag output did not match the query count")

    release_shas: list[str] = []
    seen_shas: set[str] = set()
    for (tag_name, expected_sha), line in zip(identities, lines):
        fields = line.split()
        if len(fields) == 2 and fields[1] == b"missing":
            raise ValidationError(f"release tag is unavailable locally: {tag_name}")
        if (
            len(fields) != 2
            or GIT_OBJECT_ID_RE.fullmatch(fields[0]) is None
            or fields[1] != b"commit"
        ):
            raise ValidationError(f"release tag returned invalid metadata: {tag_name}")
        tag_sha = fields[0].decode("ascii")
        if tag_sha != expected_sha:
            raise ValidationError(
                f"release tag {tag_name} resolves to {tag_sha}, expected {expected_sha}"
            )
        if expected_sha not in seen_shas:
            seen_shas.add(expected_sha)
            release_shas.append(expected_sha)
    return release_shas


def _select_release_baseline_sha(repo_root: Path, release_shas: list[str]) -> str:
    revision_input = ("\n".join(release_shas) + "\n").encode("ascii")
    selected_result = _bounded_git_output(
        repo_root,
        ["git", "rev-list", "--topo-order", "--max-count=1", "--stdin"],
        stdin_data=revision_input,
        stdout_limit=MAX_GIT_METADATA_BYTES,
        stdout_overflow_error="Git release baseline output exceeds the safety limit",
    )
    if selected_result.returncode != 0:
        raise ValidationError(
            "failed to select release history baseline: "
            f"{_git_error(selected_result)}"
        )
    selected_raw = selected_result.stdout.strip()
    if (
        GIT_OBJECT_ID_RE.fullmatch(selected_raw) is None
        or selected_raw.decode("ascii") not in set(release_shas)
    ):
        raise ValidationError("Git returned an invalid release history baseline")
    selected = selected_raw.decode("ascii")

    uncovered_result = _bounded_git_output(
        repo_root,
        ["git", "rev-list", "--max-count=1", "--stdin"],
        stdin_data=revision_input + f"^{selected}\n".encode("ascii"),
        stdout_limit=MAX_GIT_METADATA_BYTES,
        stdout_overflow_error="Git release ancestry output exceeds the safety limit",
    )
    if uncovered_result.returncode != 0:
        raise ValidationError(
            "failed to verify release history ancestry: "
            f"{_git_error(uncovered_result)}"
        )
    if uncovered_result.stdout.strip():
        raise ValidationError(
            "complete published releases do not have a single descendant baseline"
        )
    return selected


def _release_history_baseline(
    repo_root: Path,
    repository: str,
    manifest: Path,
) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    identities = _complete_release_identities(repository, _github_token())
    release_shas = _verified_release_shas(repo_root, identities)
    sha = _select_release_baseline_sha(repo_root, release_shas)
    if not _is_ancestor(repo_root, sha, "HEAD"):
        raise ValidationError(f"release baseline {sha} is not an ancestor of HEAD")
    release_manifests = _release_manifests_at_commits(
        repo_root,
        release_shas,
        manifest,
    )
    return sha, release_manifests


def _release_baseline(
    repo_root: Path,
    repository: str,
    manifest: Path,
) -> tuple[str, dict[str, Any]]:
    sha, release_manifests = _release_history_baseline(
        repo_root,
        repository,
        manifest,
    )
    manifests_by_sha = dict(release_manifests)
    previous = manifests_by_sha.get(sha)
    if previous is None:
        raise ValidationError(f"release baseline {sha} does not contain {manifest}")

    return sha, previous


def _print_git_change_summary(repo_root: Path, ref: str, manifest: Path) -> None:
    commit = _resolve_commit(repo_root, ref)
    manifest_path, _encoded_path = _manifest_git_path(manifest)
    env = os.environ.copy()
    env["GIT_LITERAL_PATHSPECS"] = "1"
    result = _bounded_git_output(
        repo_root,
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            commit,
            "--",
            manifest_path,
            "personal_codex",
            "scripts",
        ],
        env=env,
        stdout_limit=MAX_GIT_CHANGE_SUMMARY_BYTES,
        stdout_overflow_error="Git change summary exceeds the 1 MiB safety limit",
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    if result.returncode == 0 and stdout.strip():
        print("git change summary:")
        for line in stdout.splitlines()[:100]:
            print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate append-only sync manifest removal metadata."
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--manifest", default="personal_codex/public-sync-manifest.json")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--base-ref")
    baseline.add_argument("--release-repo")
    return parser


def _path_argument(raw: str, field: str) -> Path:
    if "\0" in raw:
        raise ValidationError(f"{field} must not contain embedded NUL")
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValidationError(f"{field} must be valid UTF-8") from error
    return Path(raw)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = _path_argument(args.manifest, "--manifest")
    _manifest_git_path(manifest)
    try:
        repo_root = _path_argument(args.repo_root, "--repo-root").resolve()
    except (OSError, ValueError) as error:
        raise ValidationError(f"failed to resolve --repo-root: {error}") from error
    live_root_identity = _live_worktree_root_identity(repo_root)
    current_path_kind = _live_worktree_path_kind_resolver(
        repo_root,
        live_root_identity,
    )
    current_manifest_snapshot = _read_repo_manifest_snapshot(
        repo_root,
        manifest,
        live_root_identity,
    )
    current = _parse_manifest_bytes(
        current_manifest_snapshot.payload,
        f"manifest {manifest}",
    )
    current_model = _manifest_model(current)
    current_source_context = "live working tree"

    def verify_current_snapshot() -> None:
        _verify_live_worktree_snapshot(
            repo_root,
            manifest,
            current_manifest_snapshot,
            current_path_kind,
        )

    if args.release_repo:
        base_ref, release_manifests = _release_history_baseline(
            repo_root,
            args.release_repo,
            manifest,
        )
        manifests_by_sha = dict(release_manifests)
        previous = manifests_by_sha.get(base_ref)
        if previous is None:
            raise ValidationError(
                f"release baseline {base_ref} does not contain {manifest}"
            )
        historical_models: list[tuple[str, int, dict[str, Any]]] = []
        modeled_manifest_payloads: set[int] = set()
        removed_history_floor: dict[str, dict[str, Any]] = {}
        removed_history_release_shas: dict[str, str] = {}
        for release_sha, release_manifest in release_manifests:
            payload_identity = id(release_manifest)
            if payload_identity in modeled_manifest_payloads:
                continue
            modeled_manifest_payloads.add(payload_identity)
            historical_model = _manifest_model(
                release_manifest,
                enforce_history_constraints=False,
            )
            if historical_model["owner"] != current_model["owner"]:
                raise ValidationError(
                    "manifest owner must not change between releases: "
                    f"{release_sha}"
                )
            for removed_id, historical_entry in historical_model["removed"].items():
                previous_entry = removed_history_floor.get(removed_id)
                if previous_entry is None:
                    removed_history_floor[removed_id] = historical_entry
                    removed_history_release_shas[removed_id] = release_sha
                    continue
                if previous_entry != historical_entry:
                    raise ValidationError(
                        "removed link history conflicts between releases "
                        f"{removed_history_release_shas[removed_id]} and "
                        f"{release_sha}: {removed_id}"
                    )
            historical_models.append(
                (release_sha, payload_identity, historical_model)
            )
        for release_sha, _payload_identity, historical_model in historical_models:
            _validate_removed_link_history_preserved(
                historical_model["removed"],
                current_model["removed"],
                release_sha=release_sha,
            )
        previous_path_kind = _git_tree_path_kind_resolver(repo_root, base_ref)
        validate_manifest_change(
            previous,
            current,
            known_prior_removed=removed_history_floor,
            previous_path_kind=previous_path_kind,
            current_path_kind=current_path_kind,
            previous_source_context=f"base commit {base_ref}",
            current_source_context=current_source_context,
        )
        current_targets = set(current_model["links"])
        for release_sha, payload_identity, historical_model in historical_models:
            if payload_identity == id(previous):
                continue
            _validate_transition_capacity(
                historical_model,
                current_model,
                release_sha=release_sha,
            )
            historical_targets = set(historical_model["links"])
            _validate_portable_target_spellings(
                [*historical_targets, *current_targets]
            )
            _validate_target_hierarchy_changes(
                historical_targets - current_targets,
                current_targets - historical_targets,
            )
        verify_current_snapshot()
        _print_git_change_summary(repo_root, base_ref, manifest)
        print(f"sync manifest change validation ok against release {base_ref}")
        return 0

    base_ref = args.base_ref
    if not base_ref or set(base_ref) == {"0"}:
        _manifest_model(
            current,
            current_path_kind,
            source_context=current_source_context,
        )
        verify_current_snapshot()
        print("manifest schema ok; no base ref supplied")
        return 0
    base_commit = _resolve_commit(repo_root, base_ref)
    previous = _manifest_at_ref(repo_root, base_commit, manifest)
    if previous is None:
        _manifest_model(
            current,
            current_path_kind,
            source_context=current_source_context,
        )
        verify_current_snapshot()
        print("manifest schema ok; base manifest does not exist")
        return 0
    previous_path_kind = _git_tree_path_kind_resolver(repo_root, base_commit)
    validate_manifest_change(
        previous,
        current,
        previous_path_kind=previous_path_kind,
        current_path_kind=current_path_kind,
        previous_source_context=f"base commit {base_commit}",
        current_source_context=current_source_context,
    )
    verify_current_snapshot()
    _print_git_change_summary(repo_root, base_commit, manifest)
    print("sync manifest change validation ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
