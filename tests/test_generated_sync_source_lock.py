from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import Callable, Optional
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_generated_sync_source_lock.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_generated_sync_source_lock",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("failed to load generated sync source verifier")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)

TEST_FILES = (
    (
        "engine",
        "scripts/codex_personal_sync.py",
        "scripts/codex_personal_sync.py",
        "0755",
    ),
    (
        "engine_tests",
        "tests/test_codex_personal_sync.py",
        "tests/test_codex_personal_sync.py",
        "0644",
    ),
    (
        "manifest_schema",
        "schema/sync-manifest.schema.json",
        "schema/sync-manifest.schema.json",
        "0644",
    ),
    (
        "reconciliation_safety_tests",
        "tests/test_personal_sync_reconciliation_safety.py",
        "tests/test_personal_sync_reconciliation_safety.py",
        "0644",
    ),
    (
        "release_retention_tests",
        "tests/test_release_retention.py",
        "tests/test_release_retention.py",
        "0644",
    ),
    (
        "scheduler_doctor_tests",
        "tests/test_scheduler_doctor.py",
        "tests/test_scheduler_doctor.py",
        "0644",
    ),
)


def canonical_json(value: object, *, pretty: bool) -> bytes:
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


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value, pretty=False)).hexdigest()


def refresh_digests(receipt: dict) -> None:
    files = receipt["files"]
    mapping = [
        {
            "source_name": item["source_name"],
            "source_path": item["source_path"],
            "target_path": item["target_path"],
        }
        for item in files
    ]
    file_set = sorted(item["target_path"] for item in files)
    tree = [
        {
            "target_path": item["target_path"],
            "sha256": item["sha256"],
            "mode": item["mode"],
        }
        for item in sorted(files, key=lambda item: item["target_path"])
    ]
    receipt["mapping_digest"] = digest(mapping)
    receipt["file_set_digest"] = digest(file_set)
    receipt["tree_digest"] = digest(tree)


class GeneratedSyncSourceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        files = []
        for source_name, source_path, target_path, mode in TEST_FILES:
            payload = f"fixture payload for {source_name}\n".encode("utf-8")
            path = self.root / target_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(int(mode, 8))
            files.append(
                {
                    "source_name": source_name,
                    "source_path": source_path,
                    "target_path": target_path,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "mode": mode,
                }
            )
        self.receipt = {
            "receipt_version": 1,
            "generator_contract_version": 2,
            "rules_contract_version": 1,
            "hash_algorithm": "sha256",
            "canonical_repository": "Joey-Tools/codex-personal-sync",
            "canonical_commit": "7803eebe63782f5539c22e1b7f0d7a7ec587ac3f",
            "mirror": "toolbox",
            "mirror_repository": "Joey-Tools/codex-toolbox",
            "mapping_digest": "",
            "file_set_digest": "",
            "tree_digest": "",
            "files": files,
        }
        refresh_digests(self.receipt)
        self.expected_receipt_sha256 = self.write_receipt()
        self.snapshot_root = self.root / "snapshot"
        self.snapshot_root.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_receipt(self) -> str:
        payload = canonical_json(self.receipt, pretty=True)
        receipt_path = self.root / "generated-sync-source-lock.json"
        receipt_path.write_bytes(payload)
        receipt_path.chmod(0o644)
        return hashlib.sha256(payload).hexdigest()

    def verify(
        self,
        expected_receipt_sha256: str = "",
        *,
        snapshot_root: Optional[Path] = None,
    ) -> None:
        VERIFIER.verify_generated_mirror(
            self.root,
            snapshot_root=snapshot_root or self.snapshot_root,
            expected_receipt_sha256=(
                expected_receipt_sha256 or self.expected_receipt_sha256
            ),
        )

    def _verify_with_mutation_between_descriptor_reads(
        self,
        target_path: str,
        mutation: Callable[[], None],
    ) -> None:
        original_read = VERIFIER._read_descriptor_pass
        mutated = False

        def read_then_mutate(file_fd, relative_path, *, maximum_bytes):
            nonlocal mutated
            result = original_read(
                file_fd,
                relative_path,
                maximum_bytes=maximum_bytes,
            )
            if not mutated and str(relative_path) == target_path:
                mutated = True
                mutation()
            return result

        with mock.patch.object(
            VERIFIER,
            "_read_descriptor_pass",
            side_effect=read_then_mutate,
        ):
            self.verify()

    def test_production_tree_cli_passes(self) -> None:
        snapshot_root = self.root / "production-snapshot"
        snapshot_root.mkdir(mode=0o700)
        completed = subprocess.run(
            [
                sys.executable,
                os.fspath(SCRIPT_PATH),
                "--snapshot-root",
                os.fspath(snapshot_root),
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "verified and installed generated sync source snapshot (6 files)",
            completed.stdout,
        )

    def test_valid_fixture_passes(self) -> None:
        self.verify()
        installed_files = sorted(
            path.relative_to(self.snapshot_root).as_posix()
            for path in self.snapshot_root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(
            installed_files,
            sorted(
                ["generated-sync-source-lock.json"]
                + [target_path for _, _, target_path, _ in TEST_FILES]
            ),
        )

    def test_same_repository_and_snapshot_root_passes(self) -> None:
        expected_payloads = {
            target_path: (self.root / target_path).read_bytes()
            for _, _, target_path, _ in TEST_FILES
        }

        self.verify(snapshot_root=self.root)

        for target_path, expected_payload in expected_payloads.items():
            self.assertEqual((self.root / target_path).read_bytes(), expected_payload)

    def test_rejects_receipt_external_digest_tamper(self) -> None:
        receipt_path = self.root / "generated-sync-source-lock.json"
        receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
        with self.assertRaisesRegex(
            VERIFIER.VerificationError, "external digest mismatch"
        ):
            self.verify()

    def test_rejects_canonical_commit_drift(self) -> None:
        self.receipt["canonical_commit"] = "0" * 40
        expected = self.write_receipt()
        with self.assertRaisesRegex(VERIFIER.VerificationError, "canonical_commit"):
            self.verify(expected)

    def test_rejects_canonical_repository_and_mirror_identity_drift(self) -> None:
        changed_values = (
            ("canonical_repository", "Joey-Tools/other-canonical"),
            ("mirror", "other-mirror"),
            ("mirror_repository", "Joey-Tools/other-toolbox"),
        )
        for field_name, changed_value in changed_values:
            with self.subTest(field_name=field_name):
                original = self.receipt[field_name]
                self.receipt[field_name] = changed_value
                expected = self.write_receipt()
                with self.assertRaisesRegex(VERIFIER.VerificationError, field_name):
                    self.verify(expected)
                self.receipt[field_name] = original

    def test_rejects_receipt_mode_drift(self) -> None:
        (self.root / "generated-sync-source-lock.json").chmod(0o600)
        with self.assertRaisesRegex(VERIFIER.VerificationError, "mode must be 0644"):
            self.verify()

    def test_rejects_unknown_record(self) -> None:
        self.receipt["files"][0]["source_name"] = "unknown"
        refresh_digests(self.receipt)
        expected = self.write_receipt()
        with self.assertRaisesRegex(VERIFIER.VerificationError, "source_name"):
            self.verify(expected)

    def test_rejects_extra_record(self) -> None:
        extra = dict(self.receipt["files"][-1])
        extra.update(
            {
                "source_name": "extra",
                "source_path": "tests/extra.py",
                "target_path": "tests/extra.py",
            }
        )
        self.receipt["files"].append(extra)
        refresh_digests(self.receipt)
        expected = self.write_receipt()
        with self.assertRaisesRegex(VERIFIER.VerificationError, "file count changed"):
            self.verify(expected)

    def test_rejects_missing_record(self) -> None:
        self.receipt["files"].pop()
        refresh_digests(self.receipt)
        expected = self.write_receipt()
        with self.assertRaisesRegex(VERIFIER.VerificationError, "file count changed"):
            self.verify(expected)

    def test_rejects_unknown_schema_field(self) -> None:
        self.receipt["unexpected"] = True
        expected = self.write_receipt()
        with self.assertRaisesRegex(VERIFIER.VerificationError, "fields changed"):
            self.verify(expected)

    def test_rejects_each_derived_digest_mismatch(self) -> None:
        for field_name in ("mapping_digest", "file_set_digest", "tree_digest"):
            with self.subTest(field_name=field_name):
                original = self.receipt[field_name]
                self.receipt[field_name] = "0" * 64
                expected = self.write_receipt()
                with self.assertRaisesRegex(VERIFIER.VerificationError, field_name):
                    self.verify(expected)
                self.receipt[field_name] = original

    def test_rejects_missing_managed_file(self) -> None:
        (self.root / TEST_FILES[-1][2]).unlink()
        with self.assertRaisesRegex(VERIFIER.VerificationError, "missing or unsafe"):
            self.verify()

    def test_rejects_managed_content_drift(self) -> None:
        path = self.root / TEST_FILES[-1][2]
        path.write_bytes(path.read_bytes() + b"drift\n")
        path.chmod(0o644)
        with self.assertRaisesRegex(
            VERIFIER.VerificationError, "content digest mismatch"
        ):
            self.verify()

    def test_rejects_managed_mode_drift(self) -> None:
        path = self.root / TEST_FILES[-1][2]
        path.chmod(0o600)
        with self.assertRaisesRegex(VERIFIER.VerificationError, "mode mismatch"):
            self.verify()

    def test_rejects_managed_symlink(self) -> None:
        path = self.root / TEST_FILES[-1][2]
        target = self.root / "symlink-target"
        shutil.copyfile(path, target)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(VERIFIER.VerificationError, "missing or unsafe"):
            self.verify()

    def test_rejects_non_private_snapshot_root(self) -> None:
        self.snapshot_root.chmod(0o755)
        with self.assertRaisesRegex(
            VERIFIER.VerificationError,
            "snapshot root mode must be 0700",
        ):
            self.verify()

    def test_rejects_snapshot_root_with_other_uid_replaceable_binding(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary_parent_name:
            temporary_parent = Path(temporary_parent_name).resolve()
            temporary_parent.chmod(0o777)
            snapshot_root = temporary_parent / "snapshot"
            snapshot_root.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                VERIFIER.VerificationError,
                "path binding can be replaced by another uid",
            ):
                self.verify(snapshot_root=snapshot_root)

    def test_rejects_wrong_owner_snapshot_root_when_uid_check_is_mocked(self) -> None:
        with mock.patch.object(
            VERIFIER.os,
            "getuid",
            return_value=os.getuid() + 1,
        ):
            # A shared temporary ancestor can reject the mocked UID before the
            # final snapshot-root ownership check. Both classifications prove
            # that the mismatched policy identity is rejected fail-closed.
            with self.assertRaisesRegex(
                VERIFIER.VerificationError,
                "current uid|path binding can be replaced by another uid",
            ):
                self.verify()
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_rejects_symlink_snapshot_root(self) -> None:
        target = self.root / "snapshot-target"
        target.mkdir(mode=0o700)
        symlink = self.root / "snapshot-symlink"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(
            VERIFIER.VerificationError,
            "snapshot root is missing or unsafe",
        ):
            self.verify(snapshot_root=symlink)

    def test_rejects_non_directory_snapshot_root(self) -> None:
        file_root = self.root / "snapshot-file"
        file_root.write_bytes(b"not a directory\n")
        file_root.chmod(0o700)
        with self.assertRaisesRegex(
            VERIFIER.VerificationError,
            "snapshot root is missing or unsafe",
        ):
            self.verify(snapshot_root=file_root)

    def test_cli_installation_failure_has_no_success_output(self) -> None:
        self.snapshot_root.chmod(0o755)
        completed = subprocess.run(
            [
                sys.executable,
                os.fspath(SCRIPT_PATH),
                "--repo-root",
                os.fspath(self.root),
                "--snapshot-root",
                os.fspath(self.snapshot_root),
            ],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("verified and installed", completed.stdout)
        self.assertIn("snapshot root mode must be 0700", completed.stderr)

    def test_rejects_snapshot_object_installation_failure(self) -> None:
        with mock.patch.object(
            VERIFIER.os,
            "replace",
            side_effect=OSError("injected replacement failure"),
        ):
            with self.assertRaisesRegex(
                VERIFIER.VerificationError,
                "snapshot object installation failed",
            ):
                self.verify()

        self.assertFalse(
            (self.snapshot_root / "generated-sync-source-lock.json").exists()
        )

    def test_capture_precedes_install_for_receipt_and_early_managed_path(self) -> None:
        receipt_path = self.root / "generated-sync-source-lock.json"
        target_path = TEST_FILES[0][2]
        managed_path = self.root / target_path
        expected_receipt = receipt_path.read_bytes()
        expected_receipt_mode = stat.S_IMODE(receipt_path.stat().st_mode)
        expected_managed = managed_path.read_bytes()
        expected_managed_mode = stat.S_IMODE(managed_path.stat().st_mode)
        original_install = VERIFIER._install_captured_objects

        def replace_sources_then_install(snapshot_root_fd, captured_objects):
            replacement_receipt = receipt_path.with_name("later-receipt")
            replacement_receipt.write_bytes(b"later receipt pathname bytes\n")
            replacement_receipt.chmod(0o600)
            os.replace(replacement_receipt, receipt_path)
            replacement_managed = managed_path.with_name("later-managed")
            replacement_managed.write_bytes(b"later managed pathname bytes\n")
            replacement_managed.chmod(0o600)
            os.replace(replacement_managed, managed_path)
            original_install(snapshot_root_fd, captured_objects)

        with mock.patch.object(
            VERIFIER,
            "_install_captured_objects",
            side_effect=replace_sources_then_install,
        ):
            self.verify()

        installed_receipt = self.snapshot_root / receipt_path.name
        installed_managed = self.snapshot_root / target_path
        self.assertEqual(installed_receipt.read_bytes(), expected_receipt)
        self.assertEqual(
            stat.S_IMODE(installed_receipt.stat().st_mode),
            expected_receipt_mode,
        )
        self.assertEqual(installed_managed.read_bytes(), expected_managed)
        self.assertEqual(
            stat.S_IMODE(installed_managed.stat().st_mode),
            expected_managed_mode,
        )

    def test_accepts_mtime_only_churn_between_descriptor_reads(self) -> None:
        target_path = TEST_FILES[0][2]
        path = self.root / target_path
        before = os.stat(path, follow_symlinks=False)

        def touch_mtime() -> None:
            os.utime(
                path,
                ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                follow_symlinks=False,
            )

        self._verify_with_mutation_between_descriptor_reads(target_path, touch_mtime)

        after = os.stat(path, follow_symlinks=False)
        self.assertNotEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(
            (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mode,
                after.st_uid,
                after.st_gid,
            ),
            (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mode,
                before.st_uid,
                before.st_gid,
            ),
        )

    def test_accepts_hard_link_count_churn_between_descriptor_reads(self) -> None:
        target_path = TEST_FILES[0][2]
        path = self.root / target_path
        link_path = self.root / "managed-hard-link"
        before = os.stat(path, follow_symlinks=False)

        self._verify_with_mutation_between_descriptor_reads(
            target_path,
            lambda: os.link(path, link_path),
        )

        after = os.stat(path, follow_symlinks=False)
        self.assertEqual(after.st_nlink, before.st_nlink + 1)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_rejects_object_identity_drift_during_descriptor_read(self) -> None:
        target_path = TEST_FILES[0][2]
        path = self.root / target_path
        target_inode = path.stat().st_ino
        original_fstat = VERIFIER.os.fstat
        target_calls = 0

        def fstat_with_identity_drift(file_fd):
            nonlocal target_calls
            metadata = original_fstat(file_fd)
            if stat.S_ISREG(metadata.st_mode) and metadata.st_ino == target_inode:
                target_calls += 1
                if target_calls == 2:
                    fields = list(metadata)
                    fields[stat.ST_INO] += 1
                    return os.stat_result(fields)
            return metadata

        with mock.patch.object(
            VERIFIER.os,
            "fstat",
            side_effect=fstat_with_identity_drift,
        ):
            with self.assertRaisesRegex(
                VERIFIER.VerificationError,
                "object identity changed while being read",
            ):
                self.verify()

    def test_rejects_content_drift_between_descriptor_reads(self) -> None:
        target_path = TEST_FILES[0][2]
        path = self.root / target_path
        initial_payload = path.read_bytes()

        def change_content() -> None:
            path.write_bytes(b"x" * len(initial_payload))
            path.chmod(0o755)

        with self.assertRaisesRegex(
            VERIFIER.VerificationError,
            "content changed while being read",
        ):
            self._verify_with_mutation_between_descriptor_reads(
                target_path,
                change_content,
            )

    def test_rejects_access_policy_drift_between_descriptor_reads(self) -> None:
        target_path = TEST_FILES[0][2]
        path = self.root / target_path

        with self.assertRaisesRegex(
            VERIFIER.VerificationError,
            "access policy changed while being read",
        ):
            self._verify_with_mutation_between_descriptor_reads(
                target_path,
                lambda: path.chmod(0o700),
            )


if __name__ == "__main__":
    unittest.main()
