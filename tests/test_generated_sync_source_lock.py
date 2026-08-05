from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
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
        self.root = Path(self.temporary_directory.name)
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
            "canonical_commit": "14914ca17172f00a5759758a50cf7c0295e4a42f",
            "mirror": "toolbox",
            "mirror_repository": "Joey-Tools/codex-toolbox",
            "mapping_digest": "",
            "file_set_digest": "",
            "tree_digest": "",
            "files": files,
        }
        refresh_digests(self.receipt)
        self.expected_receipt_sha256 = self.write_receipt()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_receipt(self) -> str:
        payload = canonical_json(self.receipt, pretty=True)
        receipt_path = self.root / "generated-sync-source-lock.json"
        receipt_path.write_bytes(payload)
        receipt_path.chmod(0o644)
        return hashlib.sha256(payload).hexdigest()

    def verify(self, expected_receipt_sha256: str = "") -> None:
        VERIFIER.verify_generated_mirror(
            self.root,
            expected_receipt_sha256=(
                expected_receipt_sha256 or self.expected_receipt_sha256
            ),
        )

    def test_production_tree_cli_passes(self) -> None:
        completed = subprocess.run(
            [sys.executable, os.fspath(SCRIPT_PATH)],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("verified generated sync source lock (6 files)", completed.stdout)

    def test_valid_fixture_passes(self) -> None:
        self.verify()

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

    def test_rejects_change_after_first_managed_scan(self) -> None:
        original_hash = VERIFIER._hash_bounded_regular
        target_path = TEST_FILES[0][2]
        calls = 0

        def hash_then_change(root_fd, relative_path, *, maximum_bytes):
            nonlocal calls
            result = original_hash(
                root_fd,
                relative_path,
                maximum_bytes=maximum_bytes,
            )
            if str(relative_path) == target_path:
                calls += 1
                if calls == 1:
                    path = self.root / target_path
                    path.write_bytes(path.read_bytes() + b"post-scan drift\n")
                    path.chmod(0o755)
            return result

        with mock.patch.object(
            VERIFIER,
            "_hash_bounded_regular",
            side_effect=hash_then_change,
        ):
            with self.assertRaisesRegex(
                VERIFIER.VerificationError,
                "changed before final group revalidation",
            ):
                self.verify()


if __name__ == "__main__":
    unittest.main()
