#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from typing import Iterable, Optional
import unicodedata


class GitError(RuntimeError):
    pass


class PlanError(RuntimeError):
    pass


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
class BoundTarget:
    path: Path
    relative_parts: tuple[str, ...]
    existing_nodes: tuple[BoundNode, ...]
    missing_parts: tuple[str, ...]
    collision_key: tuple[object, ...]


@dataclass
class PlannedWorktree:
    submodule: Submodule
    sha: str
    target: BoundTarget
    source_git_dir: Path
    parent_source_git_dir: Optional[Path]
    parent_index: Optional[int]
    state: str
    access_bindings: tuple[AccessBinding, ...]
    needs_fetch: bool


@dataclass
class SyncPlan:
    root: Path
    display_root: Path
    entries: list[PlannedWorktree]
    depth: int
    force_replace_empty: bool
    fetch_missing: bool


def run(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise GitError(f"{shell_join(args)} failed with exit code {result.returncode}: {stderr}")
    return result


def read_git(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(["git", "--no-optional-locks", *args], cwd=cwd, check=check)


def git(args: list[str], *, cwd: Optional[Path] = None, check: bool = True) -> str:
    return read_git(args, cwd=cwd, check=check).stdout.strip()


def shell_join(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def resolved_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def validate_relative_git_path(value: str, field: str, origin: str) -> str:
    if not value:
        raise PlanError(f"{field} in {origin} must not be empty")
    if value.startswith("/"):
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


def filesystem_fingerprint(path: Path) -> FsFingerprint:
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise PlanError(f"required path is missing: {path}") from exc
    except PermissionError as exc:
        raise PlanError(f"required path is unreadable: {path}") from exc
    return FsFingerprint(
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        kind=stat.S_IFMT(path_stat.st_mode),
        owner=path_stat.st_uid,
        group=path_stat.st_gid,
        permissions=stat.S_IMODE(path_stat.st_mode),
    )


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
        raise PlanError(
            f"{purpose} path has an unsafe object type\n"
            f"  path: {path}"
        )
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


def normalized_path_parts(parts: Iterable[str]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFD", part).casefold() for part in parts)


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


def bind_target_path(root: Path, relative_parts: tuple[str, ...], label: str) -> BoundTarget:
    root = root.resolve(strict=True)
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

    path = root.joinpath(*relative_parts)
    if missing_parts:
        anchor = nodes[-1].fingerprint
        collision_key: tuple[object, ...] = (
            "missing",
            anchor.device,
            anchor.inode,
            *normalized_path_parts(missing_parts),
        )
    else:
        target_fingerprint = nodes[-1].fingerprint
        collision_key = ("existing", target_fingerprint.device, target_fingerprint.inode)
    return BoundTarget(
        path=path,
        relative_parts=relative_parts,
        existing_nodes=tuple(nodes),
        missing_parts=missing_parts,
        collision_key=collision_key,
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
            raise PlanError(f"target path became a symlink alias/collision: {node.path}")
    if target.missing_parts:
        first_missing = target.existing_nodes[-1].path / target.missing_parts[0]
        try:
            os.stat(first_missing, follow_symlinks=False)
        except FileNotFoundError:
            return
        except PermissionError as exc:
            raise PlanError(f"target path became unreadable: {first_missing}") from exc
        raise PlanError(f"target path changed after preflight: {first_missing} now exists")


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
            raise PlanError(f"section [{section}] in {origin} is missing required keys: {exc}") from exc
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


def read_worktree_gitmodules(root: Path) -> list[Submodule]:
    path = root / ".gitmodules"
    if not path.exists():
        return []
    return parse_gitmodules(path.read_text(encoding="utf-8"), str(path))


def read_commit_gitmodules(source_git_dir: Path, work_tree: Path, commit: str) -> list[Submodule]:
    del work_tree
    tree_entry = read_git(
        [
            *source_object_repo_args(source_git_dir),
            "ls-tree",
            commit,
            "--",
            ".gitmodules",
        ]
    ).stdout.strip()
    if not tree_entry:
        return []
    fields = tree_entry.split(maxsplit=3)
    if len(fields) != 4 or fields[1] != "blob":
        raise PlanError(f"{commit}:.gitmodules is not a regular Git blob")
    content = git(
        [*source_object_repo_args(source_git_dir), "show", f"{commit}:.gitmodules"]
    )
    return parse_gitmodules(content, f"{commit}:.gitmodules")


def expected_sha(root: Path, rel_path: str) -> str:
    output = git(["ls-files", "-s", "--", rel_path], cwd=root)
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise PlanError(f"{rel_path} is not a gitlink in the current index")
    if len(lines) != 1:
        raise PlanError(f"{rel_path} has unresolved index entries; resolve conflicts before syncing")
    fields = lines[0].split()
    if len(fields) < 4 or fields[0] != "160000":
        raise PlanError(f"{rel_path} is not a gitlink in the current index")
    if fields[2] != "0":
        raise PlanError(f"{rel_path} has unresolved index stage {fields[2]}; resolve conflicts before syncing")
    return fields[1]


def expected_sha_from_tree(source_git_dir: Path, work_tree: Path, treeish: str, rel_path: str) -> str:
    output = git([*source_object_repo_args(source_git_dir), "ls-tree", treeish, "--", rel_path])
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
        [*source_object_repo_args(source_git_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
        check=False,
    )
    return result.returncode == 0


def fetch_missing_commit(
    source_git_dir: Path,
    work_tree: Path,
    submodule: Submodule,
    sha: str,
    depth: int,
    dry_run: bool,
    fetch_missing: bool = False,
) -> bool:
    if commit_exists(source_git_dir, work_tree, sha):
        return True
    command = [
        "git",
        *source_object_repo_args(source_git_dir),
        "fetch",
        "--depth",
        str(depth),
        "origin",
        sha,
    ]
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
    print(f"fetch missing commit for {submodule.path}: {shell_join(command)}", flush=True)
    result = run(command, check=False)
    if result.returncode == 0 and commit_exists(source_git_dir, work_tree, sha):
        return True
    stderr = (result.stderr or "").strip()
    branch_fetch_command = [
        "git",
        *source_object_repo_args(source_git_dir),
        "fetch",
        "--depth",
        "100",
        "origin",
        "<branch-or-tag>",
    ]
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
    result = read_git(["-C", str(worktree_path), "rev-parse", "--git-common-dir"], check=False)
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


def has_local_changes(worktree_path: Path) -> bool:
    result = read_git(["-C", str(worktree_path), "status", "--porcelain"], check=True)
    return bool(result.stdout.strip())


def registered_worktree_paths(source_git_dir: Path) -> list[Path]:
    result = read_git(
        [
            *source_object_repo_args(source_git_dir),
            "worktree",
            "list",
            "--porcelain",
            "-z",
        ]
    )
    paths: list[Path] = []
    for field in result.stdout.split("\0"):
        if field.startswith("worktree "):
            paths.append(Path(field[len("worktree ") :]).resolve(strict=False))
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


def prepare_target_path(path: Path, source_git_dir: Path, force_replace_empty: bool, dry_run: bool) -> str:
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
            raise PlanError(f"{path} is an empty directory; pass --force-replace-empty to use it")
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
    if has_local_changes(worktree_path):
        raise PlanError(f"{worktree_path} has local changes; clean it before syncing")
    command = ["git", "-C", str(worktree_path), "checkout", "--detach", sha]
    if dry_run:
        print(f"would checkout existing worktree: {shell_join(command)}")
        return
    run(command)


def add_worktree(source_git_dir: Path, worktree_path: Path, sha: str, dry_run: bool) -> None:
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


def source_access_bindings(source_git_dir: Path, needs_fetch: bool) -> list[AccessBinding]:
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
        raise PlanError(f"cannot resolve managed worktree admin directory: {target.path}")
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
        "origin",
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


def entry_has_ancestor(
    entries: list[PlannedWorktree],
    entry_parent_index: Optional[int],
    candidate_index: int,
) -> bool:
    current = entry_parent_index
    while current is not None:
        if current == candidate_index:
            return True
        current = entries[current].parent_index
    return False


def reject_plan_collision(
    entries: list[PlannedWorktree],
    candidate: PlannedWorktree,
) -> None:
    candidate_parts = normalized_path_parts(candidate.target.relative_parts)
    for prior_index, prior in enumerate(entries):
        prior_parts = normalized_path_parts(prior.target.relative_parts)
        if candidate.target.collision_key == prior.target.collision_key:
            raise PlanError(
                "planned worktree target collision\n"
                f"  first: {prior.target.path}\n"
                f"  second: {candidate.target.path}"
            )
        prior_prefix = (
            len(prior_parts) < len(candidate_parts)
            and candidate_parts[: len(prior_parts)] == prior_parts
        )
        candidate_prefix = (
            len(candidate_parts) < len(prior_parts)
            and prior_parts[: len(candidate_parts)] == candidate_parts
        )
        if prior_prefix and entry_has_ancestor(
            entries,
            candidate.parent_index,
            prior_index,
        ):
            continue
        if prior_prefix or candidate_prefix or prior_parts == candidate_parts:
            raise PlanError(
                "planned worktree targets overlap or alias each other\n"
                f"  first: {prior.target.path}\n"
                f"  second: {candidate.target.path}"
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
) -> SyncPlan:
    root = root.resolve(strict=True)
    entries: list[PlannedWorktree] = []

    def add_entry(
        submodule: Submodule,
        sha: str,
        parent_target_parts: tuple[str, ...],
        parent_source: Optional[Path],
        parent_index: Optional[int],
    ) -> None:
        module_parts = tuple(
            validate_relative_git_path(
                submodule.path,
                f"worktree path for submodule {submodule.path}",
                ".gitmodules",
            ).split("/")
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
        bindings = [
            *source_access_bindings(source_git_dir, needs_fetch),
            *target_access_bindings(target, state, source_git_dir),
        ]
        entry = PlannedWorktree(
            submodule=submodule,
            sha=sha,
            target=target,
            source_git_dir=source_git_dir,
            parent_source_git_dir=parent_source,
            parent_index=parent_index,
            state=state,
            access_bindings=tuple(bindings),
            needs_fetch=needs_fetch,
        )
        reject_plan_collision(entries, entry)
        current_index = len(entries)
        entries.append(entry)

        if not recursive:
            return
        for nested in read_commit_gitmodules(source_git_dir, target.path, sha):
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
    )


def fetch_command(entry: PlannedWorktree, depth: int) -> list[str]:
    return [
        "git",
        *source_object_repo_args(entry.source_git_dir),
        "fetch",
        "--depth",
        str(depth),
        "origin",
        entry.sha,
    ]


def print_sync_plan(plan: SyncPlan) -> None:
    for entry in plan.entries:
        display_path = relative_display_path(plan.display_root, entry.target.path)
        print(f"sync {display_path} -> {entry.sha}", flush=True)
        if entry.state == "managed":
            checkout_existing_worktree(entry.target.path, entry.sha, dry_run=True)
        else:
            if entry.state == "empty":
                print(f"would use empty directory: {entry.target.path}")
            add_worktree(entry.source_git_dir, entry.target.path, entry.sha, dry_run=True)
        if entry.needs_fetch:
            print(
                f"would fetch missing commit for {entry.submodule.path}: "
                f"{shell_join(fetch_command(entry, plan.depth))}"
            )


def revalidate_runtime_source_access(entry: PlannedWorktree) -> None:
    source_access_bindings(entry.source_git_dir, entry.needs_fetch)


def revalidate_planned_entry(
    plan: SyncPlan,
    entry: PlannedWorktree,
    *,
    allow_parent_materialization: bool = False,
) -> BoundTarget:
    for binding in entry.access_bindings:
        revalidate_access(binding)
    revalidate_runtime_source_access(entry)

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
    return target


def validate_sync_plan(plan: SyncPlan) -> None:
    for entry in plan.entries:
        revalidate_planned_entry(plan, entry)


def apply_sync_plan(plan: SyncPlan) -> None:
    validate_sync_plan(plan)
    for entry in plan.entries:
        if not entry.needs_fetch:
            continue
        validate_sync_plan(plan)
        fetch_missing_commit(
            entry.source_git_dir,
            entry.target.path,
            entry.submodule,
            entry.sha,
            plan.depth,
            dry_run=False,
            fetch_missing=True,
        )
        entry.needs_fetch = False

    validate_sync_plan(plan)
    applied_indexes: set[int] = set()
    for index, entry in enumerate(plan.entries):
        target = revalidate_planned_entry(
            plan,
            entry,
            allow_parent_materialization=(
                entry.parent_index is not None and entry.parent_index in applied_indexes
            ),
        )
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
        raise PlanError("use either explicit top-level submodule paths or --all, not both")
    if all_paths:
        return None
    if not requested_paths:
        raise PlanError("no submodule paths selected; pass explicit top-level paths or --all")

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


def choose_source_common_git_dir(args: argparse.Namespace, target_root: Path) -> tuple[Path, Optional[Path]]:
    if args.source_common_git_dir and args.source_superproject:
        raise PlanError("use only one of --source-common-git-dir or --source-superproject")
    if args.source_common_git_dir:
        return resolved_path(args.source_common_git_dir), None
    if args.source_superproject:
        source_root, _, source_common_git_dir = repo_paths(resolved_path(args.source_superproject))
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
    parser.add_argument("--repo", default=".", help="target superproject worktree; defaults to current directory")
    parser.add_argument("--depth", type=int, default=1, help="depth used when fetching a missing target commit")
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
    parser.add_argument("--force-replace-empty", action="store_true", help="allow using existing empty directories")
    parser.add_argument("--no-recursive", action="store_true", help="do not sync nested submodules")
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
    if args.depth < 1:
        raise PlanError("--depth must be greater than zero")
    normalize_requested_paths(args.paths, all_paths=args.all_paths)

    root, _, _ = repo_paths(resolved_path(args.repo))
    source_common_git_dir, source_superproject = choose_source_common_git_dir(args, root)
    modules = filter_submodules(
        read_worktree_gitmodules(root),
        args.paths,
        all_paths=args.all_paths,
    )
    planned_modules = [(module, expected_sha(root, module.path)) for module in modules]
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
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
