#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import configparser
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import selectors
import shlex
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
MAX_CHECKOUT_PATHS = 250_000
MAX_CHECKOUT_PATH_BYTES = 4096
MAX_CHECKOUT_PATH_COMPONENTS = 1_000_000
MAX_CHECKOUT_ACCESS_BINDINGS = 500_000
MAX_NAME_POLICY_PROBE_ENTRIES = 256

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
class CheckoutPreflight:
    kind: str
    current_head: Optional[str]
    index_digest: Optional[str]
    index_entry_count: Optional[int]
    path_count: int
    path_digest: str
    changes: tuple[TreeChange, ...]


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
    source_bindings: tuple[AccessBinding, ...]
    target_bindings: tuple[AccessBinding, ...]
    checkout_preflight: Optional[CheckoutPreflight]
    needs_fetch: bool


@dataclass
class SyncPlan:
    root: Path
    display_root: Path
    entries: list[PlannedWorktree]
    depth: int
    force_replace_empty: bool
    fetch_missing: bool
    name_policy: FilesystemNamePolicy


def git_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in GIT_ENV_PASSTHROUGH if key in os.environ
    }
    environment.update(SAFE_GIT_ENV)
    return environment


def safe_command(args: list[str]) -> list[str]:
    if not args or args[0] != "git":
        return args
    return ["git", *SAFE_GIT_CONFIG_ARGS, *args[1:]]


def run(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = safe_command(args)
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=git_environment(),
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


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def run_bounded_bytes(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    input_bytes: Optional[bytes] = None,
    timeout_seconds: float = GIT_ENUMERATION_TIMEOUT_SECONDS,
    stdout_limit: int = GIT_ENUMERATION_OUTPUT_LIMIT_BYTES,
    stderr_limit: int = GIT_ERROR_OUTPUT_LIMIT_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    if input_bytes is not None and len(input_bytes) > GIT_INPUT_LIMIT_BYTES:
        raise PlanError(
            f"Git command input exceeds the {GIT_INPUT_LIMIT_BYTES}-byte safety limit"
        )
    command = safe_command(args)
    input_file = tempfile.TemporaryFile()
    if input_bytes is not None:
        input_file.write(input_bytes)
        input_file.seek(0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            env=git_environment(),
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
            terminate_process_group(process)
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
            terminate_process_group(process)
            raise PlanError(f"{shell_join(command)} {failure}")
    except BaseException:
        terminate_process_group(process)
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
) -> subprocess.CompletedProcess[bytes]:
    return run_bounded_bytes(
        ["git", "--no-optional-locks", *args],
        cwd=cwd,
        check=check,
        input_bytes=input_bytes,
        stdout_limit=stdout_limit,
    )


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


def probe_directory_case_sensitive(path: Path) -> Optional[bool]:
    try:
        with os.scandir(path) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_NAME_POLICY_PROBE_ENTRIES:
                    break
                alternate_name = alternate_case_name(entry.name)
                if alternate_name is None:
                    continue
                original_path = path / entry.name
                alternate_path = path / alternate_name
                original = filesystem_fingerprint(original_path)
                try:
                    alternate_stat = os.stat(
                        alternate_path,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return True
                except PermissionError as exc:
                    raise PlanError(
                        f"cannot inspect target name semantics: {alternate_path}"
                    ) from exc
                alternate = fingerprint_from_stat(alternate_stat)
                if (
                    original.device == alternate.device
                    and original.inode == alternate.inode
                ):
                    return False
                return True
    except PermissionError as exc:
        raise PlanError(f"cannot inspect target name semantics: {path}") from exc
    return None


def linux_directory_casefold(path: Path) -> Optional[bool]:
    if not sys.platform.startswith("linux"):
        return None
    import array
    import fcntl

    fs_ioc_getflags = 0x80086601
    fs_casefold_fl = 0x40000000
    flags = array.array("I", [0])
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            fcntl.ioctl(descriptor, fs_ioc_getflags, flags, True)
        except OSError:
            return None
    finally:
        os.close(descriptor)
    return bool(flags[0] & fs_casefold_fl)


def filesystem_name_policy(root: Path) -> FilesystemNamePolicy:
    root = root.resolve(strict=True)
    configured_case_insensitive = local_git_bool(root, "core.ignoreCase")
    configured_precompose = local_git_bool(root, "core.precomposeUnicode")

    if sys.platform == "darwin":
        case_sensitive = darwin_volume_case_sensitive(root)
        return FilesystemNamePolicy(
            case_sensitive=case_sensitive,
            normalization=("exact" if configured_precompose is False else "NFD"),
            source="darwin-volume-capabilities",
        )

    if os.name == "nt":
        case_sensitive = False
    else:
        probed_case_sensitive = probe_directory_case_sensitive(root)
        if probed_case_sensitive is not None:
            case_sensitive = probed_case_sensitive
        elif configured_case_insensitive is None:
            case_sensitive = True
        else:
            case_sensitive = not configured_case_insensitive
    directory_casefold = linux_directory_casefold(root)
    if directory_casefold:
        case_sensitive = False
    normalization = "NFD" if directory_casefold or configured_precompose else "exact"
    return FilesystemNamePolicy(
        case_sensitive=case_sensitive,
        normalization=normalization,
        source="directory-filesystem-probe",
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
    if name_policy is None:
        name_policy = filesystem_name_policy(root)
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
            *normalized_path_parts(missing_parts, name_policy),
        )
    else:
        target_fingerprint = nodes[-1].fingerprint
        collision_key = (
            "existing",
            target_fingerprint.device,
            target_fingerprint.inode,
        )
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
            raise PlanError(
                f"target path became a symlink alias/collision: {node.path}"
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


def read_worktree_gitmodules(root: Path) -> list[Submodule]:
    path = root / ".gitmodules"
    if not path.exists():
        return []
    return parse_gitmodules(path.read_text(encoding="utf-8"), str(path))


def read_commit_gitmodules(
    source_git_dir: Path, work_tree: Path, commit: str
) -> list[Submodule]:
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
    print(
        f"fetch missing commit for {submodule.path}: {shell_join(command)}", flush=True
    )
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
    if has_local_changes(worktree_path):
        raise PlanError(f"{worktree_path} has local changes; clean it before syncing")
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
    run(command)


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


def managed_index_snapshot(worktree_path: Path) -> tuple[str, int]:
    result = read_git_bounded(
        ["-C", str(worktree_path), "ls-files", "--stage", "-v", "-z"]
    )
    records = bounded_records(result.stdout, "managed worktree index")
    for record in records:
        if not record.startswith(b"H "):
            tag = os.fsdecode(record[:1]) if record else "<empty>"
            raise PlanError(
                "managed worktree index has unsupported sparse or hidden state\n"
                f"  worktree: {worktree_path}\n"
                f"  tag: {tag}"
            )
    return hashlib.sha256(result.stdout).hexdigest(), len(records)


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


def materialized_blob_paths(
    source_git_dir: Path,
    target_sha: str,
    changes: Optional[tuple[TreeChange, ...]],
) -> tuple[tuple[str, ...], ...]:
    if changes is None:
        return target_tree_blob_paths(source_git_dir, target_sha)
    return tuple(
        change.relative_parts for change in changes if change.new_mode.startswith("100")
    )


def reject_checkout_filters(
    source_git_dir: Path,
    worktree_path: Path,
    target_sha: str,
    paths: tuple[tuple[str, ...], ...],
) -> None:
    if not paths:
        return
    encoded_paths = bytearray()
    for parts in paths:
        raw_path = os.fsencode("/".join(parts))
        if len(encoded_paths) + len(raw_path) + 1 > GIT_INPUT_LIMIT_BYTES:
            raise PlanError(
                "checkout attribute query exceeds the "
                f"{GIT_INPUT_LIMIT_BYTES}-byte input limit"
            )
        encoded_paths.extend(raw_path)
        encoded_paths.append(0)
    result = read_git_bounded(
        [
            *source_object_repo_args(source_git_dir),
            "check-attr",
            f"--source={target_sha}",
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
    for offset in range(0, len(records), 3):
        raw_path, attribute, value = records[offset : offset + 3]
        if attribute != b"filter":
            raise PlanError(
                "checkout filter attribute result named the wrong attribute"
            )
        if value in {b"unspecified", b"unset"}:
            continue
        raise PlanError(
            "checkout requires an untrusted content filter and is blocked before mutation\n"
            f"  worktree: {worktree_path}\n"
            f"  path: {os.fsdecode(raw_path)}\n"
            f"  filter: {os.fsdecode(value)}\n"
            "  this helper does not execute repository-defined smudge filters"
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
) -> tuple[tuple[str, ...], ...]:
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
        ]
    )
    return tuple(
        validate_checkout_path(raw_path, "ignored worktree inventory")
        for raw_path in bounded_records(
            result.stdout,
            "ignored worktree inventory",
        )
    )


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
    for ignored_parts in ignored_worktree_paths(worktree_path):
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
    if entry.state == "managed":
        if has_local_changes(entry.target.path):
            raise PlanError(
                f"{entry.target.path} has local changes; clean it before syncing"
            )
        current_head = managed_head(entry.target.path)
        index_digest, index_entry_count = managed_index_snapshot(entry.target.path)
        changes = parse_managed_tree_changes(
            entry.target.path,
            current_head,
            entry.sha,
        )
        write_bindings = checkout_write_access_bindings(
            entry.target.path,
            changes,
        )
        reject_managed_ignored_conflicts(entry.target.path, changes)
        probe_managed_checkout(entry.target.path, entry.sha)

    blob_paths = materialized_blob_paths(
        entry.source_git_dir,
        entry.sha,
        changes,
    )
    reject_checkout_filters(
        entry.source_git_dir,
        entry.target.path,
        entry.sha,
        blob_paths,
    )
    digest_paths: Iterable[tuple[str, ...]]
    if changes is None:
        digest_paths = blob_paths
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
    if has_local_changes(entry.target.path):
        raise PlanError(
            f"{entry.target.path} has local changes; clean it before syncing"
        )
    if managed_head(entry.target.path) != receipt.current_head:
        raise PlanError(
            f"managed worktree HEAD changed after preflight: {entry.target.path}"
        )
    current_digest, current_count = managed_index_snapshot(entry.target.path)
    if (
        current_digest != receipt.index_digest
        or current_count != receipt.index_entry_count
    ):
        raise PlanError(
            f"managed worktree index changed after preflight: {entry.target.path}"
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
    name_policy: FilesystemNamePolicy,
) -> None:
    candidate_parts = normalized_path_parts(
        candidate.target.relative_parts,
        name_policy,
    )
    for prior_index, prior in enumerate(entries):
        prior_parts = normalized_path_parts(
            prior.target.relative_parts,
            name_policy,
        )
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
    name_policy = filesystem_name_policy(root)
    entries: list[PlannedWorktree] = []
    source_identities: dict[tuple[int, int], tuple[Submodule, Path]] = {}

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
            name_policy,
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
            needs_fetch=needs_fetch,
        )
        if commit_available:
            checkout_preflight, write_bindings = capture_checkout_preflight(entry)
            entry.checkout_preflight = checkout_preflight
            entry.target_bindings = (*entry.target_bindings, *write_bindings)
        reject_plan_collision(entries, entry, name_policy)
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
        name_policy=name_policy,
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
            plan.name_policy,
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
    current_policy = filesystem_name_policy(plan.root)
    if current_policy != plan.name_policy:
        raise PlanError(
            "target filesystem name semantics changed after preflight\n"
            f"  root: {plan.root}"
        )
    for entry in plan.entries:
        revalidate_planned_entry(plan, entry)


def apply_sync_plan(plan: SyncPlan) -> None:
    validate_sync_plan(plan)
    for entry in plan.entries:
        if not entry.needs_fetch:
            continue
        revalidate_runtime_source_access(entry)
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

    for entry in plan.entries:
        if entry.checkout_preflight is not None:
            continue
        checkout_preflight, write_bindings = capture_checkout_preflight(entry)
        entry.checkout_preflight = checkout_preflight
        entry.target_bindings = (*entry.target_bindings, *write_bindings)

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
    if args.depth < 1:
        raise PlanError("--depth must be greater than zero")
    normalize_requested_paths(args.paths, all_paths=args.all_paths)

    root, _, _ = repo_paths(resolved_path(args.repo))
    source_common_git_dir, source_superproject = choose_source_common_git_dir(
        args, root
    )
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
