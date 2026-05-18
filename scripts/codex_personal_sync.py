#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any


TAG_PREFIX = "personal-codex-"
TAG_RE = re.compile(r"^personal-codex-\d{8}-\d{6}-([0-9a-f]{7,40})$")
ASSET_RE = re.compile(r"^personal-codex-([0-9a-f]{40})\.tar\.gz$")
SHA256_RE = re.compile(r"^personal-codex-([0-9a-f]{40})\.sha256$")
RELEASE_DIR_RE = re.compile(r"^[0-9a-f]{40}$")
MANIFEST_RELATIVE_PATH = Path("personal_codex/sync-manifest.json")
DEFAULT_RELEASE_REPO_ENV = "CODEX_PERSONAL_SYNC_DEFAULT_REPO"
LAUNCHD_LABEL = "io.github.joey-tools.codex-personal-sync"
SYSTEMD_UNIT = "codex-personal-sync"
DEFAULT_SCHEDULER_INTERVAL_MINUTES = 60
MACOS_SCHEDULER_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LINUX_SCHEDULER_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAssets:
    tag_name: str
    sha: str
    archive_name: str
    checksum_name: str


@dataclass(frozen=True)
class LinkEntry:
    source: PurePosixPath
    target: PurePosixPath
    kind: str


@dataclass(frozen=True)
class LinkAction:
    action: str
    target: Path
    link_target: str
    kind: str


@dataclass(frozen=True)
class SchedulerPaths:
    platform: str
    launchd_plist: Path | None = None
    systemd_service: Path | None = None
    systemd_timer: Path | None = None


def _display_path(path: Path) -> str:
    return str(path.expanduser())


def _validate_relative_path(raw: object, field_name: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw:
        raise SyncError(f"{field_name} must be a non-empty relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise SyncError(f"{field_name} must not be absolute or contain parent traversal: {raw}")
    if any(part in ("", ".") for part in path.parts):
        raise SyncError(f"{field_name} must not contain empty or current-dir segments: {raw}")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except OSError as error:
        raise SyncError(f"Failed to read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise SyncError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise SyncError(f"Expected JSON object in {path}")
    return data


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


def load_manifest(release_root: Path) -> list[LinkEntry]:
    manifest_path = release_root / MANIFEST_RELATIVE_PATH
    data = _load_json(manifest_path)
    if data.get("version") != 1:
        raise SyncError("sync manifest version must be 1")
    raw_links = data.get("links")
    if not isinstance(raw_links, list) or not raw_links:
        raise SyncError("sync manifest must contain a non-empty links array")

    entries: list[LinkEntry] = []
    targets: set[PurePosixPath] = set()
    for index, raw_entry in enumerate(raw_links):
        if not isinstance(raw_entry, dict):
            raise SyncError(f"manifest link #{index + 1} must be an object")
        source = _validate_relative_path(raw_entry.get("source"), "source")
        target = _validate_relative_path(raw_entry.get("target"), "target")
        kind = raw_entry.get("kind")
        if kind not in {"file", "directory", "skill"}:
            raise SyncError(f"manifest link {source} has unsupported kind: {kind}")
        if target in targets:
            raise SyncError(f"duplicate manifest target: {target}")
        targets.add(target)
        source_path = release_root / Path(*source.parts)
        if kind == "file":
            if not source_path.is_file():
                raise SyncError(f"manifest file source is missing: {source}")
        else:
            if not source_path.is_dir():
                raise SyncError(f"manifest directory source is missing: {source}")
            if kind == "skill" and not (source_path / "SKILL.md").is_file():
                raise SyncError(f"manifest skill source is missing SKILL.md: {source}")
        entries.append(LinkEntry(source=source, target=target, kind=kind))

    raw_references = data.get("reference_only", [])
    if not isinstance(raw_references, list):
        raise SyncError("reference_only must be an array when present")
    for raw_reference in raw_references:
        reference = _validate_relative_path(raw_reference, "reference_only")
        if not (release_root / Path(*reference.parts)).exists():
            raise SyncError(f"reference_only path is missing: {reference}")

    return entries


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

    archive_matches: list[tuple[str, str]] = []
    checksum_names: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        archive_match = ASSET_RE.fullmatch(name)
        if archive_match:
            archive_matches.append((archive_match.group(1), name))
            continue
        checksum_match = SHA256_RE.fullmatch(name)
        if checksum_match:
            checksum_names[checksum_match.group(1)] = name

    if not archive_matches:
        raise SyncError(f"release {tag_name} has no personal-codex tarball asset")
    if len(archive_matches) > 1:
        names = ", ".join(name for _, name in archive_matches)
        raise SyncError(f"release {tag_name} has multiple tarball assets: {names}")
    sha, archive_name = archive_matches[0]
    checksum_name = checksum_names.get(sha)
    if checksum_name is None:
        raise SyncError(f"release {tag_name} is missing checksum asset for {archive_name}")
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
    return ReleaseAssets(
        tag_name=tag_name,
        sha=sha,
        archive_name=archive_name,
        checksum_name=checksum_name,
    )


def verify_checksum(archive_path: Path, checksum_path: Path) -> None:
    expected: str | None = None
    archive_name = archive_path.name
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
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

    digest = hashlib.sha256()
    with archive_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise SyncError(
            f"checksum mismatch for {archive_name}: expected {expected}, got {actual}"
        )


def _validate_tar_member(member: tarfile.TarInfo) -> None:
    member_path = PurePosixPath(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise SyncError(f"refusing unsafe archive member path: {member.name}")
    if any(part in ("", ".") for part in member_path.parts):
        raise SyncError(f"refusing archive member path with empty/current segment: {member.name}")
    if member.issym() or member.islnk():
        raise SyncError(f"refusing archive link member: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise SyncError(f"refusing unsupported archive member type: {member.name}")
    # Releases are installed under a single user's home; keep executables usable
    # while stripping special and group/world-write bits on older Python fallback paths.
    if member.isdir():
        member.mode = (member.mode & 0o755) | 0o700
    else:
        member.mode &= 0o755


def safe_extract_archive(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise SyncError("archive is empty")
        for member in members:
            _validate_tar_member(member)
        try:
            archive.extractall(destination, filter="data")
        except TypeError:
            archive.extractall(destination, members=members)
    return find_release_root(destination)


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


def _releases_root(home: Path) -> Path:
    return _personal_sync_root(home) / "releases"


def _current_link(home: Path) -> Path:
    return _personal_sync_root(home) / "current"


def _install_lock_path(home: Path) -> Path:
    return _personal_sync_root(home) / "install.lock"


@contextlib.contextmanager
def installation_lock(home: Path):
    lock_path = _install_lock_path(home)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _entry_target_path(home: Path, entry: LinkEntry) -> Path:
    return home / Path(*entry.target.parts)


def _entry_current_source(home: Path, entry: LinkEntry) -> Path:
    return _current_link(home) / Path(*entry.source.parts)


def _desired_link_target(home: Path, entry: LinkEntry) -> str:
    target_path = _entry_target_path(home, entry)
    source_path = _entry_current_source(home, entry)
    return os.path.relpath(source_path, start=target_path.parent)


def _path_exists_or_is_link(path: Path) -> bool:
    return os.path.lexists(path)


def plan_link_actions(home: Path, entries: list[LinkEntry]) -> list[LinkAction]:
    actions: list[LinkAction] = []
    for entry in entries:
        target = _entry_target_path(home, entry)
        desired = _desired_link_target(home, entry)
        parent = target.parent
        if _path_exists_or_is_link(parent) and not parent.is_dir():
            raise SyncError(f"link parent exists but is not a directory: {parent}")
        if _path_exists_or_is_link(target):
            if not target.is_symlink():
                raise SyncError(f"refusing to replace non-symlink target: {target}")
            existing = os.readlink(target)
            if existing == desired:
                continue
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


def current_release_entries(home: Path) -> list[LinkEntry]:
    sha = _current_sha(home)
    if sha is None:
        return []
    release_root = _releases_root(home) / sha
    if not (release_root / MANIFEST_RELATIVE_PATH).is_file():
        return []
    return validate_release_tree(release_root)


def plan_stale_link_removals(
    home: Path,
    previous_entries: list[LinkEntry],
    next_entries: list[LinkEntry],
) -> list[LinkAction]:
    next_targets = {entry.target for entry in next_entries}
    removals: list[LinkAction] = []
    for entry in previous_entries:
        if entry.target in next_targets:
            continue
        target = _entry_target_path(home, entry)
        if target.is_symlink() and os.readlink(target) == _desired_link_target(home, entry):
            removals.append(LinkAction("remove", target, "", entry.kind))
    return removals


def _known_manifest_target_parents(home: Path, entries: list[LinkEntry]) -> set[Path]:
    parents = {home, home / "agents", home / "bin", home / "skills"}
    parents.update(_entry_target_path(home, entry).parent for entry in entries)
    releases_root = _releases_root(home)
    if not releases_root.is_dir():
        return parents
    for release_dir in releases_root.iterdir():
        if not release_dir.is_dir() or not (release_dir / MANIFEST_RELATIVE_PATH).is_file():
            continue
        try:
            release_entries = validate_release_tree(release_dir)
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

    releases_root = _releases_root(home)
    if not releases_root.is_dir():
        return targets
    for release_dir in releases_root.iterdir():
        if (
            not release_dir.is_dir()
            or RELEASE_DIR_RE.fullmatch(release_dir.name) is None
            or not (release_dir / MANIFEST_RELATIVE_PATH).is_file()
        ):
            continue
        try:
            release_entries = validate_release_tree(release_dir)
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
    current_root = _current_link(home).resolve(strict=False)
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


def _copy_release_tree(source_root: Path, release_dir: Path) -> None:
    releases_root = release_dir.parent
    tmp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".tmp-{release_dir.name}-",
            dir=str(releases_root),
        )
    )
    try:
        shutil.copytree(source_root, tmp_dir, dirs_exist_ok=True)
        os.replace(tmp_dir, release_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _switch_current(home: Path, sha: str, *, dry_run: bool) -> None:
    sync_root = _personal_sync_root(home)
    current = _current_link(home)
    if _path_exists_or_is_link(current) and not current.is_symlink():
        raise SyncError(f"refusing to replace non-symlink current pointer: {current}")
    if dry_run:
        print(f"would switch {current} -> releases/{sha}")
        return
    tmp_current = sync_root / f".current-{sha}-{os.getpid()}"
    if _path_exists_or_is_link(tmp_current):
        tmp_current.unlink()
    tmp_current.symlink_to(Path("releases") / sha, target_is_directory=True)
    os.replace(tmp_current, current)
    print(f"switched {current} -> releases/{sha}")


def install_release_tree(source_root: Path, home: Path, sha: str, *, dry_run: bool) -> None:
    home = home.expanduser()
    entries = validate_release_tree(source_root)
    actions = plan_link_actions(home, entries)
    sync_root = _personal_sync_root(home)
    releases_root = _releases_root(home)
    release_dir = releases_root / sha

    if dry_run:
        previous_entries = current_release_entries(home)
        stale_removals = plan_stale_link_removals(home, previous_entries, entries)
        repair_removals = plan_stale_current_link_removals(home, entries)
        removals = _dedupe_link_actions([*stale_removals, *repair_removals])
        print(f"would install release {sha} into {release_dir}")
        _switch_current(home, sha, dry_run=True)
        apply_link_actions(actions, dry_run=True)
        apply_link_actions(removals, dry_run=True)
        if not actions and not removals:
            print("all managed symlinks already point at current")
        return

    home.mkdir(parents=True, exist_ok=True)
    sync_root.mkdir(parents=True, exist_ok=True)
    releases_root.mkdir(parents=True, exist_ok=True)
    with installation_lock(home):
        previous_entries = current_release_entries(home)
        actions = plan_link_actions(home, entries)
        stale_removals = plan_stale_link_removals(home, previous_entries, entries)
        if release_dir.exists():
            validate_release_tree(release_dir)
            print(f"release already present: {release_dir}")
        else:
            _copy_release_tree(source_root, release_dir)
            print(f"installed release tree: {release_dir}")
        _switch_current(home, sha, dry_run=False)
        apply_link_actions(actions, dry_run=False)
        apply_link_actions(stale_removals, dry_run=False)
        repair_removals = plan_stale_current_link_removals(home, entries)
        apply_link_actions(repair_removals, dry_run=False)
        if not actions and not stale_removals and not repair_removals:
            print("all managed symlinks already point at current")


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
        return json.loads(completed.stdout)
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


def download_release_assets(repo: str, assets: ReleaseAssets, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for asset_name in (assets.archive_name, assets.checksum_name):
        _run_gh(
            [
                "release",
                "download",
                assets.tag_name,
                "--repo",
                repo,
                "--pattern",
                asset_name,
                "--dir",
                str(destination),
            ]
        )


def install_from_github(repo: str, home: Path, *, dry_run: bool) -> None:
    release = find_latest_release(repo)
    assets = select_release_assets(release)
    with tempfile.TemporaryDirectory(prefix="codex-personal-sync.") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        download_release_assets(repo, assets, temp_dir)
        archive_path = temp_dir / assets.archive_name
        checksum_path = temp_dir / assets.checksum_name
        verify_checksum(archive_path, checksum_path)
        extract_root = temp_dir / "extract"
        release_root = safe_extract_archive(archive_path, extract_root)
        install_release_tree(release_root, home, assets.sha, dry_run=dry_run)


def _current_sha(home: Path) -> str | None:
    current = _current_link(home)
    if not current.is_symlink():
        return None
    target = PurePosixPath(os.readlink(current))
    parts = target.parts
    if len(parts) == 2 and parts[0] == "releases":
        return parts[1]
    resolved = current.resolve(strict=False)
    releases_root = _releases_root(home).resolve(strict=False)
    try:
        return resolved.relative_to(releases_root).parts[0]
    except ValueError:
        return None


def status(home: Path) -> None:
    home = home.expanduser()
    sha = _current_sha(home)
    if sha is None:
        print(f"not installed under {_display_path(home)}")
        return
    release_root = _releases_root(home) / sha
    manifest_path = release_root / MANIFEST_RELATIVE_PATH
    if not manifest_path.is_file():
        print(f"current pointer is broken: missing {manifest_path}")
        return
    entries = validate_release_tree(release_root)
    actions = plan_link_actions(home, entries)
    stale_removals = plan_stale_current_link_removals(home, entries)
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


def _valid_release_dirs(releases_root: Path) -> list[Path]:
    releases: list[Path] = []
    for path in releases_root.iterdir():
        if not path.is_dir() or RELEASE_DIR_RE.fullmatch(path.name) is None:
            continue
        if not (path / MANIFEST_RELATIVE_PATH).is_file():
            continue
        try:
            validate_release_tree(path)
        except SyncError:
            continue
        releases.append(path)
    return releases


def _resolve_release_for_rollback(home: Path, to_sha: str | None) -> str:
    releases_root = _releases_root(home)
    if not releases_root.is_dir():
        raise SyncError(f"release root is missing: {releases_root}")
    releases = _valid_release_dirs(releases_root)
    if to_sha:
        matches = [path.name for path in releases if path.name.startswith(to_sha)]
        if not matches:
            raise SyncError(f"no release matches {to_sha}")
        if len(matches) > 1:
            raise SyncError(f"release prefix is ambiguous: {to_sha}")
        return matches[0]

    current = _current_sha(home)
    candidates = sorted(releases, key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.name != current:
            return candidate.name
    raise SyncError("no previous release is available")


def rollback(home: Path, to_sha: str | None) -> None:
    home = home.expanduser()
    if not _releases_root(home).is_dir():
        raise SyncError(f"release root is missing: {_releases_root(home)}")
    with installation_lock(home):
        sha = _resolve_release_for_rollback(home, to_sha)
        current = _current_sha(home)
        release_root = _releases_root(home) / sha
        entries = validate_release_tree(release_root)
        previous_entries = current_release_entries(home)
        actions = plan_link_actions(home, entries)
        stale_removals = plan_stale_link_removals(home, previous_entries, entries)
        if sha != current:
            _switch_current(home, sha, dry_run=False)
        apply_link_actions(actions, dry_run=False)
        apply_link_actions(stale_removals, dry_run=False)
        repair_removals = plan_stale_current_link_removals(home, entries)
        apply_link_actions(repair_removals, dry_run=False)
        if not actions and not stale_removals and not repair_removals:
            print("all managed symlinks already point at current")


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


def _scheduler_log_dir(home: Path) -> Path:
    return _personal_sync_root(home.expanduser()) / "logs"


def _scheduler_install_args(runner: Path, repo: str, home: Path) -> list[str]:
    return [str(runner), "install", "--repo", repo, "--home", str(home.expanduser())]


def _launchd_plist(home: Path, repo: str, interval_minutes: int, runner: Path) -> dict[str, Any]:
    log_dir = _scheduler_log_dir(home)
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": _scheduler_install_args(runner, repo, home),
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "codex-personal-sync.out.log"),
        "StandardErrorPath": str(log_dir / "codex-personal-sync.err.log"),
        "EnvironmentVariables": {"PATH": MACOS_SCHEDULER_PATH},
    }


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_service(home: Path, repo: str, runner: Path) -> str:
    exec_start = " ".join(
        _systemd_quote(arg) for arg in _scheduler_install_args(runner, repo, home)
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
) -> None:
    if interval_minutes < 1:
        raise SyncError("scheduler interval must be at least 1 minute")
    home = home.expanduser()
    selected_platform = _scheduler_platform(platform_name)
    runner_path = _scheduler_runner(home, runner)
    _validate_scheduler_runner(runner_path, dry_run=dry_run)
    paths = _scheduler_paths(selected_platform, home)
    if selected_platform == "macos":
        assert paths.launchd_plist is not None
        _write_plist(
            paths.launchd_plist,
            _launchd_plist(home, repo, interval_minutes, runner_path),
            dry_run=dry_run,
        )
        if not dry_run:
            _scheduler_log_dir(home).mkdir(parents=True, exist_ok=True)
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
            _systemd_service(home, repo, runner_path),
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


def default_release_repo() -> str | None:
    return os.environ.get(DEFAULT_RELEASE_REPO_ENV)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install personal Codex config from GitHub release assets."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    release_repo = default_release_repo()

    install_parser = subparsers.add_parser("install", help="Download and install latest release")
    install_parser.add_argument("--repo", default=release_repo, required=release_repo is None)
    install_parser.add_argument("--home", default="~/.codex")
    install_parser.add_argument("--dry-run", action="store_true")

    status_parser = subparsers.add_parser("status", help="Show current release and link state")
    status_parser.add_argument("--home", default="~/.codex")

    rollback_parser = subparsers.add_parser("rollback", help="Switch current to an older release")
    rollback_parser.add_argument("--home", default="~/.codex")
    rollback_parser.add_argument("--to", help="Exact or unique release SHA prefix")

    scheduler_parser = subparsers.add_parser(
        "install-scheduler",
        help="Install a user-level scheduler that periodically runs install",
    )
    scheduler_parser.add_argument("--repo", default=release_repo, required=release_repo is None)
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
        elif args.command == "status":
            status(Path(args.home))
        elif args.command == "rollback":
            rollback(Path(args.home), args.to)
        elif args.command == "install-scheduler":
            install_scheduler(
                Path(args.home),
                args.repo,
                args.interval_minutes,
                args.platform,
                args.runner,
                dry_run=args.dry_run,
                enable=not args.no_enable,
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
