#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Iterable, Optional


class GitError(RuntimeError):
    pass


class PlanError(RuntimeError):
    pass


class Submodule:
    def __init__(self, name: str, path: str, url: str) -> None:
        self.name = name
        self.path = path
        self.url = url


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


def git(args: list[str], *, cwd: Optional[Path] = None, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


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
        modules.append(Submodule(name=name, path=path, url=url))
    return modules


def read_worktree_gitmodules(root: Path) -> list[Submodule]:
    path = root / ".gitmodules"
    if not path.exists():
        return []
    return parse_gitmodules(path.read_text(encoding="utf-8"), str(path))


def read_commit_gitmodules(source_git_dir: Path, work_tree: Path, commit: str) -> list[Submodule]:
    result = run(
        ["git", *source_object_repo_args(source_git_dir), "show", f"{commit}:.gitmodules"],
        check=False,
    )
    if result.returncode != 0:
        return []
    return parse_gitmodules(result.stdout, f"{commit}:.gitmodules")


def expected_sha(root: Path, rel_path: str) -> str:
    output = git(["ls-files", "-s", "--", rel_path], cwd=root)
    fields = output.split()
    if len(fields) < 4 or fields[0] != "160000":
        raise PlanError(f"{rel_path} is not a gitlink in the current index")
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
    result = run(
        ["git", *source_object_repo_args(source_git_dir), "rev-parse", "--git-dir"],
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
    result = run(
        ["git", *source_object_repo_args(source_git_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
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
    if dry_run:
        print(f"would fetch missing commit for {submodule.path}: {shell_join(command)}")
        return False
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
    result = run(["git", "-C", str(worktree_path), "rev-parse", "--git-common-dir"], check=False)
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
    result = run(["git", "-C", str(worktree_path), "status", "--porcelain"], check=True)
    return bool(result.stdout.strip())


def prepare_target_path(path: Path, source_git_dir: Path, force_replace_empty: bool, dry_run: bool) -> str:
    if not path.exists():
        return "missing"

    if is_managed_linked_worktree(path, source_git_dir):
        return "managed"

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
) -> None:
    worktree_path = contained_child_path(
        parent_root,
        submodule.path,
        f"worktree path for submodule {submodule.path}",
    )
    display_path = relative_display_path(root, worktree_path)
    source_git_dir = (
        nested_source_git_dir_for(parent_source_git_dir, submodule.name)
        if parent_source_git_dir
        else source_git_dir_for(common_git_dir, submodule.name)
    )

    print(f"sync {display_path} -> {sha}", flush=True)
    ensure_source_repo(source_git_dir, worktree_path, submodule, source_superproject, parent_source_git_dir)
    commit_available = fetch_missing_commit(source_git_dir, worktree_path, submodule, sha, depth, dry_run)

    state = prepare_target_path(worktree_path, source_git_dir, force_replace_empty, dry_run)
    if state == "managed":
        checkout_existing_worktree(worktree_path, sha, dry_run)
    else:
        add_worktree(source_git_dir, worktree_path, sha, dry_run)

    if not recursive:
        return
    if not commit_available:
        raise PlanError(
            f"cannot plan nested submodules for {submodule.path} in dry-run because {sha} is missing locally\n"
            "  rerun without --dry-run to fetch the target commit, fetch it manually, or pass --no-recursive"
        )
    for nested in read_commit_gitmodules(source_git_dir, worktree_path, sha):
        nested_sha = expected_sha_from_tree(source_git_dir, worktree_path, sha, nested.path)
        sync_one(
            root=root,
            common_git_dir=common_git_dir,
            source_superproject=source_superproject,
            parent_source_git_dir=source_git_dir,
            parent_root=worktree_path,
            submodule=nested,
            sha=nested_sha,
            depth=depth,
            recursive=True,
            force_replace_empty=force_replace_empty,
            dry_run=dry_run,
        )


def filter_submodules(modules: list[Submodule], requested_paths: list[str]) -> list[Submodule]:
    if not requested_paths:
        return modules
    normalized_paths = [path.rstrip("/") for path in requested_paths]
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
    return target_common_git_dir, target_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync submodules as detached linked worktrees that reuse .git/modules source repositories."
    )
    parser.add_argument("paths", nargs="*", help="top-level submodule paths to sync; defaults to all")
    parser.add_argument("--repo", default=".", help="target superproject worktree; defaults to current directory")
    parser.add_argument("--depth", type=int, default=1, help="depth used when fetching a missing target commit")
    parser.add_argument("--dry-run", action="store_true", help="print actions without changing worktrees")
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.depth < 1:
        raise PlanError("--depth must be greater than zero")

    root, _, _ = repo_paths(resolved_path(args.repo))
    source_common_git_dir, source_superproject = choose_source_common_git_dir(args, root)
    modules = filter_submodules(read_worktree_gitmodules(root), args.paths)
    for module in modules:
        sha = expected_sha(root, module.path)
        sync_one(
            root=root,
            common_git_dir=source_common_git_dir,
            source_superproject=source_superproject,
            parent_source_git_dir=None,
            parent_root=root,
            submodule=module,
            sha=sha,
            depth=args.depth,
            recursive=not args.no_recursive,
            force_replace_empty=args.force_replace_empty,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
