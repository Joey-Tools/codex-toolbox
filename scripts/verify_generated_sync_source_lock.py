#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
)


REPOSITORY_ROOT = Path(os.path.abspath(__file__)).parent.parent
RECEIPT_PATH = PurePosixPath("generated-sync-source-lock.json")
EXPECTED_RECEIPT_SHA256 = (
    "822a1d96512043149d9b95311a6009be69ac12a12821b38ec470349e9e5bfbd1"
)
EXPECTED_CANONICAL_COMMIT = "b4e74d7f35226801483a63ebe605b1298d60dc8e"
EXPECTED_CANONICAL_REPOSITORY = "Joey-Tools/codex-personal-sync"
EXPECTED_MIRROR = "toolbox"
EXPECTED_MIRROR_REPOSITORY = "Joey-Tools/codex-toolbox"
EXPECTED_RECEIPT_VERSION = 1
EXPECTED_GENERATOR_CONTRACT_VERSION = 2
EXPECTED_RULES_CONTRACT_VERSION = 1
EXPECTED_HASH_ALGORITHM = "sha256"
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_MANAGED_FILE_BYTES = 32 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

# This mapping is deliberately consumer-owned.  The receipt cannot authorize a
# new, removed, reordered, or redirected generated path by changing itself.
EXPECTED_FILES: Tuple[Mapping[str, str], ...] = (
    {
        "source_name": "engine",
        "source_path": "scripts/codex_personal_sync.py",
        "target_path": "scripts/codex_personal_sync.py",
        "mode": "0755",
    },
    {
        "source_name": "engine_tests",
        "source_path": "tests/test_codex_personal_sync.py",
        "target_path": "tests/test_codex_personal_sync.py",
        "mode": "0644",
    },
    {
        "source_name": "manifest_schema",
        "source_path": "schema/sync-manifest.schema.json",
        "target_path": "schema/sync-manifest.schema.json",
        "mode": "0644",
    },
    {
        "source_name": "reconciliation_safety_tests",
        "source_path": "tests/test_personal_sync_reconciliation_safety.py",
        "target_path": "tests/test_personal_sync_reconciliation_safety.py",
        "mode": "0644",
    },
    {
        "source_name": "release_retention_tests",
        "source_path": "tests/test_release_retention.py",
        "target_path": "tests/test_release_retention.py",
        "mode": "0644",
    },
    {
        "source_name": "scheduler_doctor_tests",
        "source_path": "tests/test_scheduler_doctor.py",
        "target_path": "tests/test_scheduler_doctor.py",
        "mode": "0644",
    },
)

TOP_LEVEL_FIELDS = frozenset(
    {
        "receipt_version",
        "generator_contract_version",
        "rules_contract_version",
        "hash_algorithm",
        "canonical_repository",
        "canonical_commit",
        "mirror",
        "mirror_repository",
        "mapping_digest",
        "file_set_digest",
        "tree_digest",
        "files",
    }
)
FILE_FIELDS = frozenset({"source_name", "source_path", "target_path", "sha256", "mode"})


class VerificationError(RuntimeError):
    pass


class _ProtectedMetadata(NamedTuple):
    object_identity: Tuple[int, int]
    content_size: int
    access_policy: Tuple[int, int, int]


class _CapturedObject(NamedTuple):
    relative_path: PurePosixPath
    payload: bytes
    mode: int


def _fail(message: str) -> NoReturn:
    raise VerificationError(message)


def _canonical_json(value: object, *, pretty: bool) -> bytes:
    if pretty:
        encoded = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    else:
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
    return (encoded + ("\n" if pretty else "")).encode("utf-8")


def _deterministic_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value, pretty=False)).hexdigest()


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"provenance receipt contains duplicate key: {key}")
        result[key] = value
    return result


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset, label: str
) -> None:
    observed = frozenset(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        _fail(f"{label} fields changed (missing={missing}, extra={extra})")


def _open_flags(*, directory: bool) -> int:
    required_names = ("O_CLOEXEC", "O_NOFOLLOW")
    if directory:
        required_names = required_names + ("O_DIRECTORY",)
    missing = [name for name in required_names if not hasattr(os, name)]
    if missing:
        _fail("platform lacks required no-follow open flags: " + ", ".join(missing))
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _open_root(repo_root: Path) -> int:
    try:
        root_fd = os.open(os.fspath(repo_root), _open_flags(directory=True))
    except OSError as error:
        raise VerificationError(
            f"repository root is missing or unsafe: {error}"
        ) from error
    try:
        metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("repository root is not a directory")
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd


def _open_regular_at(
    root_fd: int, relative_path: PurePosixPath
) -> Tuple[int, os.stat_result]:
    if relative_path.is_absolute() or not relative_path.parts:
        _fail(f"managed path is not relative: {relative_path}")
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        _fail(f"managed path has an unsafe component: {relative_path}")

    directory_fd = os.dup(root_fd)
    try:
        for component in relative_path.parts[:-1]:
            try:
                child_fd = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise VerificationError(
                    f"managed path parent is missing or unsafe: {relative_path}: {error}"
                ) from error
            os.close(directory_fd)
            directory_fd = child_fd
        try:
            file_fd = os.open(
                relative_path.parts[-1],
                _open_flags(directory=False),
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise VerificationError(
                f"managed path is missing or unsafe: {relative_path}: {error}"
            ) from error
    finally:
        os.close(directory_fd)

    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"managed path is not a regular file: {relative_path}")
    except BaseException:
        os.close(file_fd)
        raise
    return file_fd, metadata


def _protected_metadata(metadata: os.stat_result) -> _ProtectedMetadata:
    # Descriptor-stable capture protects three properties: device/inode object
    # identity, byte content (size plus two equal reads of the same descriptor),
    # and the mode/ownership access policy. Link count is not object identity,
    # and timestamps record metadata events rather than a protected property,
    # so benign hard-link or touch churn does not invalidate a stable object.
    return _ProtectedMetadata(
        object_identity=(metadata.st_dev, metadata.st_ino),
        content_size=metadata.st_size,
        access_policy=(
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
        ),
    )


def _read_descriptor_pass(
    file_fd: int,
    relative_path: PurePosixPath,
    *,
    maximum_bytes: int,
) -> bytes:
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                _fail(f"managed path exceeds byte limit: {relative_path}")
        return b"".join(chunks)
    except VerificationError:
        raise
    except OSError as error:
        raise VerificationError(
            f"managed path could not be read: {relative_path}: {error}"
        ) from error


def _require_stable_metadata(
    before: os.stat_result,
    after: os.stat_result,
    relative_path: PurePosixPath,
) -> None:
    protected_before = _protected_metadata(before)
    protected_after = _protected_metadata(after)
    if protected_before.object_identity != protected_after.object_identity:
        _fail(f"managed path object identity changed while being read: {relative_path}")
    if protected_before.content_size != protected_after.content_size:
        _fail(f"managed path content size changed while being read: {relative_path}")
    if protected_before.access_policy != protected_after.access_policy:
        _fail(f"managed path access policy changed while being read: {relative_path}")


def _capture_bounded_regular(
    root_fd: int,
    relative_path: PurePosixPath,
    *,
    maximum_bytes: int,
) -> Tuple[bytes, os.stat_result]:
    file_fd, before = _open_regular_at(root_fd, relative_path)
    try:
        if before.st_size > maximum_bytes:
            _fail(f"managed path exceeds byte limit: {relative_path}")
        first_payload = _read_descriptor_pass(
            file_fd,
            relative_path,
            maximum_bytes=maximum_bytes,
        )
        between = os.fstat(file_fd)
        _require_stable_metadata(before, between, relative_path)
        second_payload = _read_descriptor_pass(
            file_fd,
            relative_path,
            maximum_bytes=maximum_bytes,
        )
        after = os.fstat(file_fd)
        _require_stable_metadata(before, after, relative_path)
        if len(first_payload) != before.st_size or first_payload != second_payload:
            _fail(f"managed path content changed while being read: {relative_path}")
        return first_payload, before
    except VerificationError:
        raise
    except OSError as error:
        raise VerificationError(
            f"managed path could not be captured: {relative_path}: {error}"
        ) from error
    finally:
        os.close(file_fd)


def _parse_receipt(payload: bytes) -> Dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except VerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise VerificationError(
            f"provenance receipt is not valid JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        _fail("provenance receipt must be an object")
    receipt: Dict[str, Any] = raw
    _require_exact_fields(receipt, TOP_LEVEL_FIELDS, "provenance receipt")
    if payload != _canonical_json(receipt, pretty=True):
        _fail("provenance receipt is not in canonical generator encoding")

    exact_values = {
        "receipt_version": EXPECTED_RECEIPT_VERSION,
        "generator_contract_version": EXPECTED_GENERATOR_CONTRACT_VERSION,
        "rules_contract_version": EXPECTED_RULES_CONTRACT_VERSION,
        "hash_algorithm": EXPECTED_HASH_ALGORITHM,
        "canonical_repository": EXPECTED_CANONICAL_REPOSITORY,
        "canonical_commit": EXPECTED_CANONICAL_COMMIT,
        "mirror": EXPECTED_MIRROR,
        "mirror_repository": EXPECTED_MIRROR_REPOSITORY,
    }
    for field_name, expected in exact_values.items():
        observed = receipt[field_name]
        if isinstance(expected, int) and type(observed) is not int:
            _fail(f"provenance receipt {field_name} has the wrong type")
        if observed != expected:
            _fail(
                f"provenance receipt {field_name} must be {expected!r}, not {observed!r}"
            )

    for digest_name in ("mapping_digest", "file_set_digest", "tree_digest"):
        digest = receipt[digest_name]
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            _fail(f"provenance receipt {digest_name} is not lowercase SHA-256")

    raw_files = receipt["files"]
    if not isinstance(raw_files, list):
        _fail("provenance receipt files must be an array")
    if len(raw_files) != len(EXPECTED_FILES):
        _fail(
            "provenance receipt file count changed: "
            f"expected {len(EXPECTED_FILES)}, observed {len(raw_files)}"
        )

    validated_files: List[Dict[str, str]] = []
    for index, (raw_file, expected_file) in enumerate(zip(raw_files, EXPECTED_FILES)):
        if not isinstance(raw_file, dict):
            _fail(f"provenance receipt files[{index}] must be an object")
        _require_exact_fields(
            raw_file, FILE_FIELDS, f"provenance receipt files[{index}]"
        )
        for field_name in ("source_name", "source_path", "target_path", "mode"):
            observed = raw_file[field_name]
            expected = expected_file[field_name]
            if observed != expected:
                _fail(
                    f"provenance receipt files[{index}].{field_name} must be "
                    f"{expected!r}, not {observed!r}"
                )
        sha256 = raw_file["sha256"]
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            _fail(f"provenance receipt files[{index}].sha256 is not lowercase SHA-256")
        validated_files.append(dict(raw_file))

    mapping = [
        {
            "source_name": item["source_name"],
            "source_path": item["source_path"],
            "target_path": item["target_path"],
        }
        for item in validated_files
    ]
    file_set = sorted(item["target_path"] for item in validated_files)
    tree = [
        {
            "target_path": item["target_path"],
            "sha256": item["sha256"],
            "mode": item["mode"],
        }
        for item in sorted(validated_files, key=lambda item: item["target_path"])
    ]
    expected_digests = {
        "mapping_digest": _deterministic_digest(mapping),
        "file_set_digest": _deterministic_digest(file_set),
        "tree_digest": _deterministic_digest(tree),
    }
    for field_name, expected in expected_digests.items():
        if receipt[field_name] != expected:
            _fail(f"provenance receipt {field_name} does not match its files")
    return receipt


def _open_private_snapshot_root(snapshot_root: Path) -> int:
    if not hasattr(os, "getuid"):
        _fail("platform lacks the uid check required for a private snapshot root")
    snapshot_root = Path(snapshot_root)
    if not snapshot_root.is_absolute() or snapshot_root != Path(
        os.path.normpath(os.fspath(snapshot_root))
    ):
        _fail("snapshot root must be an absolute normalized path")

    current_uid = os.getuid()
    try:
        directory_fd = os.open(os.path.sep, _open_flags(directory=True))
    except OSError as error:
        raise VerificationError(
            f"snapshot root ancestry could not be opened safely: {error}"
        ) from error
    try:
        for component in snapshot_root.parts[1:]:
            parent_metadata = os.fstat(directory_fd)
            try:
                child_fd = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise VerificationError(
                    f"snapshot root is missing or unsafe: {error}"
                ) from error
            child_metadata = os.fstat(child_fd)

            trusted_uids = {0, current_uid}
            if parent_metadata.st_uid not in trusted_uids:
                os.close(child_fd)
                _fail(
                    "snapshot root path parent is not controlled by root or "
                    "the current uid"
                )
            parent_mode = stat.S_IMODE(parent_metadata.st_mode)
            writable_by_other_uid = parent_mode & (stat.S_IWGRP | stat.S_IWOTH)
            sticky_binding = (
                parent_mode & stat.S_ISVTX and child_metadata.st_uid in trusted_uids
            )
            if writable_by_other_uid and not sticky_binding:
                os.close(child_fd)
                _fail("snapshot root path binding can be replaced by another uid")

            os.close(directory_fd)
            directory_fd = child_fd

        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("snapshot root is not a directory")
        observed_mode = stat.S_IMODE(metadata.st_mode)
        if observed_mode != 0o700:
            _fail(f"snapshot root mode must be 0700: observed {observed_mode:04o}")
        if metadata.st_uid != current_uid:
            _fail(
                "snapshot root must be owned by the current uid: "
                f"expected {current_uid}, observed {metadata.st_uid}"
            )
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _open_snapshot_parent(
    snapshot_root_fd: int,
    relative_path: PurePosixPath,
) -> int:
    if relative_path.is_absolute() or not relative_path.parts:
        _fail(f"snapshot path is not relative: {relative_path}")
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        _fail(f"snapshot path has an unsafe component: {relative_path}")

    directory_fd = os.dup(snapshot_root_fd)
    try:
        for component in relative_path.parts[:-1]:
            try:
                child_fd = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise VerificationError(
                        "snapshot parent could not be created: "
                        f"{relative_path}: {error}"
                    ) from error
                try:
                    child_fd = os.open(
                        component,
                        _open_flags(directory=True),
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise VerificationError(
                        "snapshot parent is missing or unsafe after creation: "
                        f"{relative_path}: {error}"
                    ) from error
            except OSError as error:
                raise VerificationError(
                    f"snapshot parent is missing or unsafe: {relative_path}: {error}"
                ) from error
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _write_all(file_fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError("snapshot write made no progress")
        remaining = remaining[written:]


def _install_captured_object(
    snapshot_root_fd: int,
    captured: _CapturedObject,
) -> None:
    parent_fd = _open_snapshot_parent(snapshot_root_fd, captured.relative_path)
    temporary_name: Optional[str] = None
    temporary_fd: Optional[int] = None
    try:
        required_names = ("O_CLOEXEC", "O_NOFOLLOW")
        missing = [name for name in required_names if not hasattr(os, name)]
        if missing:
            _fail("platform lacks required snapshot write flags: " + ", ".join(missing))
        write_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        for _ in range(128):
            candidate = f".generated-snapshot-{secrets.token_hex(16)}"
            try:
                temporary_fd = os.open(
                    candidate,
                    write_flags,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_fd is None or temporary_name is None:
            _fail(
                f"could not allocate snapshot temporary file: {captured.relative_path}"
            )

        _write_all(temporary_fd, captured.payload)
        os.fchmod(temporary_fd, captured.mode)
        os.fsync(temporary_fd)
        metadata = os.fstat(temporary_fd)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"snapshot temporary object is not regular: {captured.relative_path}")
        if metadata.st_size != len(captured.payload):
            _fail(
                f"snapshot temporary object has the wrong size: {captured.relative_path}"
            )
        if stat.S_IMODE(metadata.st_mode) != captured.mode:
            _fail(
                f"snapshot temporary object has the wrong mode: {captured.relative_path}"
            )
        if metadata.st_uid != os.getuid():
            _fail(
                f"snapshot temporary object has the wrong owner: {captured.relative_path}"
            )

        os.replace(
            temporary_name,
            captured.relative_path.parts[-1],
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
    except VerificationError:
        raise
    except OSError as error:
        raise VerificationError(
            f"snapshot object installation failed: {captured.relative_path}: {error}"
        ) from error
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _install_captured_objects(
    snapshot_root_fd: int,
    captured_objects: Sequence[_CapturedObject],
) -> None:
    expected_paths = (RECEIPT_PATH,) + tuple(
        PurePosixPath(item["target_path"]) for item in EXPECTED_FILES
    )
    observed_paths = tuple(item.relative_path for item in captured_objects)
    if observed_paths != expected_paths:
        _fail(
            "captured snapshot object set changed: "
            f"expected {expected_paths}, observed {observed_paths}"
        )

    # The mode-0700 root excludes other uids. It does not prevent a malicious
    # or cooperative same-uid process from changing the workspace after this
    # installation succeeds, so callers must keep consumers serialized behind
    # success and must not share the workspace with concurrent writers.
    metadata = os.fstat(snapshot_root_fd)
    if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
        _fail("snapshot root privacy changed before installation")
    for captured in captured_objects:
        _install_captured_object(snapshot_root_fd, captured)


def verify_generated_mirror(
    repo_root: Path,
    *,
    snapshot_root: Path,
    expected_receipt_sha256: str = EXPECTED_RECEIPT_SHA256,
) -> None:
    if (
        not isinstance(expected_receipt_sha256, str)
        or SHA256_RE.fullmatch(expected_receipt_sha256) is None
    ):
        _fail("expected receipt digest is not lowercase SHA-256")

    snapshot_root_fd = _open_private_snapshot_root(Path(snapshot_root))
    try:
        root_fd = _open_root(Path(repo_root))
        try:
            receipt_payload, receipt_metadata = _capture_bounded_regular(
                root_fd,
                RECEIPT_PATH,
                maximum_bytes=MAX_RECEIPT_BYTES,
            )
            if stat.S_IMODE(receipt_metadata.st_mode) != 0o644:
                _fail("provenance receipt mode must be 0644")
            observed_receipt_sha256 = hashlib.sha256(receipt_payload).hexdigest()
            if observed_receipt_sha256 != expected_receipt_sha256:
                _fail(
                    "provenance receipt external digest mismatch: "
                    f"expected {expected_receipt_sha256}, "
                    f"observed {observed_receipt_sha256}"
                )
            receipt = _parse_receipt(receipt_payload)

            captured_objects = [
                _CapturedObject(
                    relative_path=RECEIPT_PATH,
                    payload=receipt_payload,
                    mode=stat.S_IMODE(receipt_metadata.st_mode),
                )
            ]
            for item in receipt["files"]:
                target_path = PurePosixPath(item["target_path"])
                payload, metadata = _capture_bounded_regular(
                    root_fd,
                    target_path,
                    maximum_bytes=MAX_MANAGED_FILE_BYTES,
                )
                expected_mode = int(item["mode"], 8)
                observed_mode = stat.S_IMODE(metadata.st_mode)
                if observed_mode != expected_mode:
                    _fail(
                        f"managed path mode mismatch: {target_path}: "
                        f"expected {expected_mode:04o}, "
                        f"observed {observed_mode:04o}"
                    )
                observed_sha256 = hashlib.sha256(payload).hexdigest()
                if observed_sha256 != item["sha256"]:
                    _fail(
                        f"managed path content digest mismatch: {target_path}: "
                        f"expected {item['sha256']}, observed {observed_sha256}"
                    )
                captured_objects.append(
                    _CapturedObject(
                        relative_path=target_path,
                        payload=payload,
                        mode=observed_mode,
                    )
                )

            # Capture is complete before the first snapshot write. Consumers
            # read these receipt-consistent bytes, never a later pathname lookup
            # in the mutable source checkout. repo_root and snapshot_root may
            # intentionally name the same private checkout because all seven
            # payloads are resident in memory before installation begins.
            _install_captured_objects(snapshot_root_fd, captured_objects)
        finally:
            os.close(root_fd)
    finally:
        os.close(snapshot_root_fd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the receipt-bound generated personal-sync mirror."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="toolbox repository root (defaults to this script's repository)",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        required=True,
        help=(
            "pre-existing current-uid mode-0700 workspace that receives the "
            "verified receipt and six managed files"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        verify_generated_mirror(args.repo_root, snapshot_root=args.snapshot_root)
    except (VerificationError, OSError) as error:
        print(f"generated sync source verification failed: {error}", file=sys.stderr)
        return 1
    print(
        "verified and installed generated sync source snapshot "
        f"({len(EXPECTED_FILES)} files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
