#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
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
    "04b8be42769d63872ba0643dcf593f3956a0ec88f42014aa39a00c24e13bdc07"
)
EXPECTED_CANONICAL_COMMIT = "14914ca17172f00a5759758a50cf7c0295e4a42f"
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
    # Revalidation protects three properties: device/inode object identity,
    # byte content (the size here plus the payload/digest at each scan), and
    # the mode/ownership access policy. Link count is not object identity, and
    # timestamps record metadata events rather than any protected property, so
    # benign hard-link or touch churn must not invalidate an otherwise stable
    # object.
    return _ProtectedMetadata(
        object_identity=(metadata.st_dev, metadata.st_ino),
        content_size=metadata.st_size,
        access_policy=(
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
        ),
    )


def _read_bounded_regular(
    root_fd: int,
    relative_path: PurePosixPath,
    *,
    maximum_bytes: int,
) -> Tuple[bytes, os.stat_result]:
    file_fd, before = _open_regular_at(root_fd, relative_path)
    try:
        if before.st_size > maximum_bytes:
            _fail(f"managed path exceeds byte limit: {relative_path}")
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
        after = os.fstat(file_fd)
        if total != before.st_size or _protected_metadata(
            before
        ) != _protected_metadata(after):
            _fail(f"managed path changed while being read: {relative_path}")
        return b"".join(chunks), before
    finally:
        os.close(file_fd)


def _hash_bounded_regular(
    root_fd: int,
    relative_path: PurePosixPath,
    *,
    maximum_bytes: int,
) -> Tuple[str, os.stat_result]:
    file_fd, before = _open_regular_at(root_fd, relative_path)
    try:
        if before.st_size > maximum_bytes:
            _fail(f"managed path exceeds byte limit: {relative_path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(file_fd, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                _fail(f"managed path exceeds byte limit: {relative_path}")
        after = os.fstat(file_fd)
        if total != before.st_size or _protected_metadata(
            before
        ) != _protected_metadata(after):
            _fail(f"managed path changed while being hashed: {relative_path}")
        return digest.hexdigest(), before
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


def verify_generated_mirror(
    repo_root: Path,
    *,
    expected_receipt_sha256: str = EXPECTED_RECEIPT_SHA256,
) -> None:
    if (
        not isinstance(expected_receipt_sha256, str)
        or SHA256_RE.fullmatch(expected_receipt_sha256) is None
    ):
        _fail("expected receipt digest is not lowercase SHA-256")

    root_fd = _open_root(Path(repo_root))
    try:
        receipt_payload, receipt_metadata = _read_bounded_regular(
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
                f"expected {expected_receipt_sha256}, observed {observed_receipt_sha256}"
            )
        receipt = _parse_receipt(receipt_payload)

        # Preserve the first scan's object identity, content digest, and access
        # policy. A complete second pass below binds scan-to-final-check
        # stability for the receipt and every managed file as one fixed group.
        first_observations: Dict[str, Tuple[str, _ProtectedMetadata]] = {}
        for item in receipt["files"]:
            target_path = PurePosixPath(item["target_path"])
            observed_sha256, metadata = _hash_bounded_regular(
                root_fd,
                target_path,
                maximum_bytes=MAX_MANAGED_FILE_BYTES,
            )
            expected_mode = int(item["mode"], 8)
            observed_mode = stat.S_IMODE(metadata.st_mode)
            if observed_mode != expected_mode:
                _fail(
                    f"managed path mode mismatch: {target_path}: "
                    f"expected {expected_mode:04o}, observed {observed_mode:04o}"
                )
            if observed_sha256 != item["sha256"]:
                _fail(
                    f"managed path content digest mismatch: {target_path}: "
                    f"expected {item['sha256']}, observed {observed_sha256}"
                )
            first_observations[item["target_path"]] = (
                observed_sha256,
                _protected_metadata(metadata),
            )

        final_receipt_payload, final_receipt_metadata = _read_bounded_regular(
            root_fd,
            RECEIPT_PATH,
            maximum_bytes=MAX_RECEIPT_BYTES,
        )
        if (
            final_receipt_payload != receipt_payload
            or _protected_metadata(final_receipt_metadata)
            != _protected_metadata(receipt_metadata)
            or stat.S_IMODE(final_receipt_metadata.st_mode) != 0o644
        ):
            _fail("provenance receipt changed before final group revalidation")

        for item in receipt["files"]:
            target_path = PurePosixPath(item["target_path"])
            final_sha256, final_metadata = _hash_bounded_regular(
                root_fd,
                target_path,
                maximum_bytes=MAX_MANAGED_FILE_BYTES,
            )
            final_observation = (
                final_sha256,
                _protected_metadata(final_metadata),
            )
            if final_observation != first_observations[item["target_path"]]:
                _fail(
                    "managed path changed before final group revalidation: "
                    f"{target_path}"
                )
    finally:
        os.close(root_fd)


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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        verify_generated_mirror(args.repo_root)
    except VerificationError as error:
        print(f"generated sync source verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified generated sync source lock ({len(EXPECTED_FILES)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
