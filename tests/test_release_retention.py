from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_release_retention",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def write_release(release_root: Path, *, marker: str) -> MODULE.ManifestData:
    skill_root = release_root / "personal_codex" / "skills" / "retention"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"# Retention\n\n{marker}\n",
        encoding="utf-8",
    )
    manifest_path = release_root / MODULE.MANIFEST_RELATIVE_PATH
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": MODULE.PUBLIC_OWNER,
                "links": [
                    {
                        "source": "personal_codex/skills/retention",
                        "target": "skills/retention",
                        "kind": "skill",
                        "owner": MODULE.PUBLIC_OWNER,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return MODULE.load_manifest_data(release_root)


def tree_snapshot(
    root: Path,
) -> tuple[tuple[str, str, int, tuple[int, int], bytes | str | None], ...]:
    if not os.path.lexists(root):
        return ()
    entries: list[tuple[str, str, int, tuple[int, int], bytes | str | None]] = []

    def visit(path: Path) -> None:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        identity = (metadata.st_dev, metadata.st_ino)
        if stat.S_ISLNK(metadata.st_mode):
            entries.append((relative, "symlink", mode, identity, os.readlink(path)))
            return
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode, identity, None))
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        if stat.S_ISREG(metadata.st_mode):
            entries.append((relative, "file", mode, identity, path.read_bytes()))
            return
        entries.append((relative, "other", mode, identity, None))

    visit(root)
    return tuple(entries)


class ReleaseRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="codex-personal-sync-retention."
        )
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_quietly(self, callback, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return callback(*args, **kwargs)

    def install_release(self, sha: str, marker: str) -> Path:
        source = self.root / f"source-{sha[0]}"
        write_release(source, marker=marker)
        self.run_quietly(
            MODULE.install_release_tree,
            source,
            self.home,
            sha,
            dry_run=False,
        )
        return source

    def install_pair(self) -> tuple[Path, Path]:
        return (
            self.install_release(SHA_A, "release-a"),
            self.install_release(SHA_B, "release-b"),
        )

    def release_path(self, sha: str) -> Path:
        return MODULE._releases_root(self.home, MODULE.PUBLIC_OWNER) / sha

    def release_identity(self, sha: str) -> tuple[int, int]:
        metadata = self.release_path(sha).lstat()
        return metadata.st_dev, metadata.st_ino

    def interrupt_before_move(self) -> MODULE.ReleaseRetentionTransaction:
        with (
            mock.patch.object(
                MODULE,
                "_atomic_move_beneath_home",
                side_effect=MODULE.SyncError("injected before-move failure"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "injected before-move failure"),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)
        transaction = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertFalse(transaction.committed)
        return transaction

    def interrupt_before_commit(self) -> MODULE.ReleaseRetentionTransaction:
        with (
            mock.patch.object(
                MODULE,
                "_publish_release_retention_commit_marker",
                side_effect=MODULE.SyncError("injected precommit failure"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "injected precommit failure"),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)
        transaction = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertFalse(transaction.committed)
        return transaction

    def interrupt_after_commit(self) -> MODULE.ReleaseRetentionTransaction:
        with (
            mock.patch.object(
                MODULE,
                "_delete_quarantined_release",
                side_effect=MODULE.SyncError("injected postcommit failure"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "injected postcommit failure"),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)
        transaction = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.committed)
        return transaction

    def stage_pending_transition(
        self,
        next_source: Path,
        next_sha: str,
    ) -> MODULE.PendingLinkBatch:
        next_manifest = MODULE.load_manifest_data(next_source)
        binding = MODULE._stage_release_tree_for_install(
            next_source,
            self.home,
            next_sha,
            next_manifest,
        )
        MODULE._close_install_release_bindings([binding])
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
        current_manifest = MODULE._current_manifest_data(
            self.home,
            MODULE.PUBLIC_OWNER,
        )
        actions = MODULE._plan_reconciliation(
            self.home,
            next_manifest.entries,
            current_manifest.entries,
            [],
            state,
            allow_cross_owner=False,
        )
        current_action = MODULE._plan_current_switch_action(
            self.home,
            next_sha,
            MODULE.PUBLIC_OWNER,
        )
        self.assertIsNotNone(current_action)
        assert current_action is not None
        managed_targets = MODULE._managed_targets_after_reconciliation(
            self.home,
            state,
            actions,
        )
        next_state = MODULE._planned_committed_state(
            self.home,
            next_manifest.entries,
            {MODULE.PUBLIC_OWNER: next_sha},
            managed_targets,
        )
        batch = MODULE._stage_pending_link_batch(
            self.home,
            [("current", [current_action]), ("managed", actions)],
            next_manifest.entries,
            {MODULE.PUBLIC_OWNER: next_sha},
            state_snapshot,
            state,
            next_state,
        )
        MODULE._publish_pending_link_pointer(self.home, batch)
        return batch

    def test_prune_moves_exact_candidate_through_quarantine_and_ignores_mtime(
        self,
    ) -> None:
        self.install_pair()
        candidate = self.release_path(SHA_A)
        expected_identity = self.release_identity(SHA_A)
        original_mtime = candidate.stat().st_mtime_ns
        touched_mtime: list[int] = []
        real_create_batch = MODULE._create_release_retention_batch
        real_move = MODULE._atomic_move_beneath_home
        real_delete = MODULE._delete_quarantined_release

        def touch_candidate_then_create(home: Path):
            changed_mtime = original_mtime + 2_000_000_000
            os.utime(candidate, ns=(changed_mtime, changed_mtime))
            touched_mtime.append(candidate.stat().st_mtime_ns)
            self.assertEqual(self.release_identity(SHA_A), expected_identity)
            return real_create_batch(home)

        with (
            mock.patch.object(
                MODULE,
                "_create_release_retention_batch",
                side_effect=touch_candidate_then_create,
            ),
            mock.patch.object(
                MODULE,
                "_atomic_move_beneath_home",
                wraps=real_move,
            ) as move,
            mock.patch.object(
                MODULE,
                "_delete_quarantined_release",
                wraps=real_delete,
            ) as delete,
        ):
            removed = self.run_quietly(
                MODULE.prune_releases,
                self.home,
                dry_run=False,
            )

        self.assertEqual(removed, [(MODULE.PUBLIC_OWNER, SHA_A)])
        self.assertEqual(len(touched_mtime), 1)
        self.assertNotEqual(touched_mtime[0], original_mtime)
        self.assertFalse(candidate.exists())
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)
        move.assert_called_once()
        move_call = move.call_args
        self.assertEqual(move_call.args[1], candidate)
        self.assertEqual(
            move_call.kwargs["expected_entry_identity"],
            expected_identity,
        )
        quarantine_destination = move_call.args[2]
        self.assertEqual(
            quarantine_destination.parent.parent.parent,
            MODULE._personal_sync_root(self.home)
            / MODULE.RELEASE_RETENTION_QUARANTINE_RELATIVE_PATH,
        )
        delete.assert_called_once_with(
            self.home,
            quarantine_destination,
            expected_identity,
            move_call.kwargs["expected_destination_parent_identity"],
        )
        self.assertFalse(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )

    def test_current_and_ledger_owner_and_link_references_are_preserved(
        self,
    ) -> None:
        self.install_pair()

        references = MODULE._retained_release_references(self.home)

        self.assertEqual(
            references[(MODULE.PUBLIC_OWNER, SHA_B)],
            {"current", "ledger", "ledger-link"},
        )
        self.assertNotIn((MODULE.PUBLIC_OWNER, SHA_A), references)
        removed = self.run_quietly(
            MODULE.prune_releases,
            self.home,
            dry_run=False,
        )
        self.assertEqual(removed, [(MODULE.PUBLIC_OWNER, SHA_A)])
        self.assertTrue(self.release_path(SHA_B).is_dir())

    def test_strict_pending_before_and_after_release_records_are_preserved(
        self,
    ) -> None:
        self.install_release(SHA_A, "release-a")
        next_source = self.root / "source-b"
        write_release(next_source, marker="release-b")
        batch = self.stage_pending_transition(next_source, SHA_B)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            {(item.owner, item.sha) for item in parsed.releases_before},
            {(MODULE.PUBLIC_OWNER, SHA_A)},
        )
        self.assertEqual(
            {(item.owner, item.sha) for item in parsed.releases_after},
            {(MODULE.PUBLIC_OWNER, SHA_B)},
        )
        references = MODULE._retained_release_references(self.home)
        self.assertIn(
            "recovery-record",
            references[(MODULE.PUBLIC_OWNER, SHA_A)],
        )
        self.assertEqual(
            references[(MODULE.PUBLIC_OWNER, SHA_B)],
            {"recovery-record"},
        )

        removed = self.run_quietly(
            MODULE.prune_releases,
            self.home,
            dry_run=False,
        )

        self.assertEqual(removed, [])
        self.assertTrue(self.release_path(SHA_A).is_dir())
        self.assertTrue(self.release_path(SHA_B).is_dir())
        self.assertTrue(batch.batch_root.is_dir())
        self.assertTrue(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_user_pin_preserves_unreferenced_release(self) -> None:
        self.install_pair()
        self.run_quietly(
            MODULE.pin_release,
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
        )

        references = MODULE._retained_release_references(self.home)
        self.assertEqual(
            references[(MODULE.PUBLIC_OWNER, SHA_A)],
            {"user-pin"},
        )
        removed = self.run_quietly(
            MODULE.prune_releases,
            self.home,
            dry_run=False,
        )

        self.assertEqual(removed, [])
        self.assertTrue(self.release_path(SHA_A).is_dir())
        self.assertTrue(
            MODULE._release_pin_path(
                self.home,
                MODULE.PUBLIC_OWNER,
                SHA_A,
            ).is_file()
        )

    def test_replacement_race_fails_closed_and_retains_recovery_evidence(
        self,
    ) -> None:
        self.install_pair()
        candidate = self.release_path(SHA_A)
        expected_identity = self.release_identity(SHA_A)
        displaced = candidate.with_name(f"{candidate.name}.displaced")
        replacement_identity: list[tuple[int, int]] = []
        real_create_batch = MODULE._create_release_retention_batch

        def replace_candidate_then_create(home: Path):
            candidate.rename(displaced)
            shutil.copytree(displaced, candidate)
            replacement_identity.append(self.release_identity(SHA_A))
            return real_create_batch(home)

        with (
            mock.patch.object(
                MODULE,
                "_create_release_retention_batch",
                side_effect=replace_candidate_then_create,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "source object changed after planning",
            ),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)

        self.assertEqual(len(replacement_identity), 1)
        self.assertNotEqual(replacement_identity[0], expected_identity)
        self.assertEqual(self.release_identity(SHA_A), replacement_identity[0])
        displaced_metadata = displaced.lstat()
        self.assertEqual(
            (displaced_metadata.st_dev, displaced_metadata.st_ino),
            expected_identity,
        )
        transaction = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction.source_identity, expected_identity)
        self.assertFalse(transaction.committed)
        with self.assertRaisesRegex(MODULE.SyncError, "recovery is ambiguous"):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=False,
            )
        self.assertTrue(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )

    def test_pre_move_record_recovery_runs_before_pin_mutation(self) -> None:
        self.install_pair()
        candidate_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_before_move()

        self.assertEqual(
            MODULE._release_entry_identity(self.release_path(SHA_A)),
            candidate_identity,
        )
        self.assertIsNone(MODULE._quarantined_release_path(self.home, transaction))
        references = MODULE._retained_release_references(self.home)
        self.assertEqual(
            references[(MODULE.PUBLIC_OWNER, SHA_A)],
            {"recovery-record"},
        )

        self.run_quietly(
            MODULE.pin_release,
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_B,
        )

        self.assertEqual(self.release_identity(SHA_A), candidate_identity)
        self.assertFalse(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )
        self.assertTrue(
            MODULE._release_pin_path(
                self.home,
                MODULE.PUBLIC_OWNER,
                SHA_B,
            ).is_file()
        )

    def test_precommit_quarantine_recovery_restores_exact_object_before_rollback(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_before_commit()

        self.assertFalse(self.release_path(SHA_A).exists())
        quarantined = MODULE._quarantined_release_path(self.home, transaction)
        self.assertIsNotNone(quarantined)
        assert quarantined is not None
        quarantined_metadata = quarantined.lstat()
        self.assertEqual(
            (quarantined_metadata.st_dev, quarantined_metadata.st_ino),
            expected_identity,
        )

        self.run_quietly(
            MODULE.rollback,
            self.home,
            SHA_A,
            MODULE.PUBLIC_OWNER,
        )

        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertEqual(MODULE._current_sha(self.home), SHA_A)
        self.assertFalse(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )

    def test_postcommit_deletion_recovery_runs_before_pin_mutation(self) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_after_commit()

        self.assertFalse(self.release_path(SHA_A).exists())
        quarantined = MODULE._quarantined_release_path(self.home, transaction)
        self.assertIsNotNone(quarantined)
        assert quarantined is not None
        quarantined_metadata = quarantined.lstat()
        self.assertEqual(
            (quarantined_metadata.st_dev, quarantined_metadata.st_ino),
            expected_identity,
        )

        self.run_quietly(
            MODULE.pin_release,
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_B,
        )

        self.assertFalse(self.release_path(SHA_A).exists())
        self.assertFalse(quarantined.exists())
        self.assertFalse(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )
        self.assertTrue(
            MODULE._release_pin_path(
                self.home,
                MODULE.PUBLIC_OWNER,
                SHA_B,
            ).is_file()
        )

    def test_clear_recovery_finishes_after_record_deleted_before_batch_rmdir(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_before_move()
        pointer = MODULE._release_retention_pointer_path(self.home)
        clear_marker = MODULE._release_retention_clear_marker_path(self.home)
        record = transaction.batch_root / MODULE.RELEASE_RETENTION_RECORD_NAME
        real_rmdir = MODULE.os.rmdir
        injected = False

        def retain_empty_batch(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args,
            **kwargs,
        ) -> None:
            nonlocal injected
            if path == transaction.batch_root.name and not injected:
                injected = True
                raise MODULE.SyncError("injected batch-rmdir failure")
            real_rmdir(
                path,
                *args,
                **kwargs,
            )

        with (
            mock.patch.object(
                MODULE.os,
                "rmdir",
                side_effect=retain_empty_batch,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected batch-rmdir failure",
            ),
        ):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=False,
            )

        self.assertTrue(injected)
        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertFalse(os.path.lexists(pointer))
        self.assertTrue(clear_marker.is_file())
        self.assertTrue(transaction.batch_root.is_dir())
        self.assertFalse(record.exists())
        self.assertFalse(transaction.destination.parent.exists())
        clearing = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(clearing)
        assert clearing is not None
        self.assertTrue(clearing.clearing)
        self.assertEqual(
            clearing.batch_root_identity,
            transaction.batch_root_identity,
        )

        recovered = MODULE._recover_release_retention_transaction(
            self.home,
            dry_run=False,
        )

        self.assertTrue(recovered)
        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertFalse(transaction.batch_root.exists())
        self.assertFalse(os.path.lexists(pointer))
        self.assertFalse(os.path.lexists(clear_marker))
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))

    def test_clear_marker_only_recovery_preserves_exact_canonical_object(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_before_move()
        pointer = MODULE._release_retention_pointer_path(self.home)
        clear_marker = MODULE._release_retention_clear_marker_path(self.home)
        real_delete_file = MODULE._delete_retention_file

        def retain_final_clear_marker(
            home: Path,
            path: Path,
            expected_identity: tuple[int, int],
            *,
            label: str,
        ) -> None:
            if path == clear_marker:
                raise MODULE.SyncError("injected clear-marker retention")
            real_delete_file(
                home,
                path,
                expected_identity,
                label=label,
            )

        with (
            mock.patch.object(
                MODULE,
                "_delete_retention_file",
                side_effect=retain_final_clear_marker,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected clear-marker retention",
            ),
        ):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=False,
            )

        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertFalse(os.path.lexists(pointer))
        self.assertTrue(clear_marker.is_file())
        self.assertFalse(transaction.batch_root.exists())
        clearing = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(clearing)
        assert clearing is not None
        self.assertTrue(clearing.clearing)
        self.assertIsNone(clearing.batch_root_identity)

        self.run_quietly(
            MODULE.unpin_release,
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_B,
        )

        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertFalse(os.path.lexists(pointer))
        self.assertFalse(os.path.lexists(clear_marker))
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))

    def test_retention_evidence_delete_tolerates_mtime_only_churn(self) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        self.interrupt_before_move()
        pointer = MODULE._release_retention_pointer_path(self.home)
        real_delete_file = MODULE._delete_retention_file
        touched = False

        def touch_pointer_before_delete(
            home: Path,
            path: Path,
            expected: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            nonlocal touched
            if path == pointer and not touched:
                touched = True
                metadata = path.stat()
                os.utime(
                    path,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000_000,
                    ),
                )
            real_delete_file(
                home,
                path,
                expected,
                label=label,
            )

        with mock.patch.object(
            MODULE,
            "_delete_retention_file",
            side_effect=touch_pointer_before_delete,
        ):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=False,
            )

        self.assertTrue(touched)
        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))

    def test_retention_evidence_byte_drift_is_preserved_fail_closed(self) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_before_move()
        pointer = MODULE._release_retention_pointer_path(self.home)
        clear_marker = MODULE._release_retention_clear_marker_path(self.home)
        real_delete_file = MODULE._delete_retention_file
        mutated = False

        def mutate_pointer_before_delete(
            home: Path,
            path: Path,
            expected: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            nonlocal mutated
            if path == pointer and not mutated:
                mutated = True
                payload = bytearray(path.read_bytes())
                payload[-2] = ord(" ")
                path.write_bytes(payload)
            real_delete_file(
                home,
                path,
                expected,
                label=label,
            )

        with (
            mock.patch.object(
                MODULE,
                "_delete_retention_file",
                side_effect=mutate_pointer_before_delete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before deletion",
            ),
        ):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=False,
            )

        self.assertTrue(mutated)
        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertTrue(pointer.is_file())
        self.assertTrue(clear_marker.is_file())
        self.assertTrue(transaction.batch_root.is_dir())
        with self.assertRaises(MODULE.SyncError):
            MODULE._load_release_retention_transaction(self.home)

    def test_malformed_retention_record_fails_closed_with_evidence_intact(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_before_move()
        pointer = MODULE._release_retention_pointer_path(self.home)
        record = transaction.batch_root / MODULE.RELEASE_RETENTION_RECORD_NAME
        pointer.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported fields",
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)

        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertTrue(pointer.is_file())
        self.assertTrue(record.is_file())
        self.assertEqual(pointer.stat().st_ino, record.stat().st_ino)
        self.assertEqual(pointer.read_bytes(), b"{}\n")
        self.assertEqual(record.read_bytes(), b"{}\n")
        self.assertTrue(transaction.batch_root.is_dir())

    def test_orphan_retention_batch_fails_closed_without_deleting_candidate(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        retention_root = (
            MODULE._personal_sync_root(self.home)
            / MODULE.RELEASE_RETENTION_QUARANTINE_RELATIVE_PATH
        )
        orphan = retention_root / (f"{MODULE.RELEASE_RETENTION_BATCH_PREFIX}orphan")
        orphan.mkdir(parents=True, mode=0o700)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not match the stable pointer",
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)

        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertTrue(orphan.is_dir())
        self.assertFalse(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )

    def test_dry_run_lists_candidate_without_mutating_tree(self) -> None:
        self.install_pair()
        before = tree_snapshot(self.home)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            removed = MODULE.prune_releases(self.home, dry_run=True)

        self.assertEqual(removed, [(MODULE.PUBLIC_OWNER, SHA_A)])
        self.assertIn(
            f"would quarantine and delete unreferenced release: "
            f"{MODULE.PUBLIC_OWNER}@{SHA_A}",
            output.getvalue(),
        )
        self.assertEqual(tree_snapshot(self.home), before)
        self.assertTrue(self.release_path(SHA_A).is_dir())
        self.assertFalse(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )

    def test_dry_run_uninstalled_home_is_read_only(self) -> None:
        for label, create_home in (
            ("absent", False),
            ("uninstalled", True),
        ):
            candidate_home = self.root / f"{label}-home"
            if create_home:
                candidate_home.mkdir()
            before = tree_snapshot(self.root)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                removed = MODULE.prune_releases(
                    candidate_home,
                    dry_run=True,
                )

            self.assertEqual(removed, [])
            self.assertIn("no installed personal sync releases", output.getvalue())
            self.assertEqual(tree_snapshot(self.root), before)
            self.assertFalse(os.path.lexists(candidate_home / "personal-sync"))

    def test_dry_run_partial_sync_root_does_not_create_install_lock(self) -> None:
        candidate_home = self.root / "partial-home"
        sync_root = candidate_home / "personal-sync"
        sync_root.mkdir(parents=True, mode=0o700)
        lock_path = sync_root / "install.lock"
        before = tree_snapshot(candidate_home)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            removed = MODULE.prune_releases(candidate_home, dry_run=True)

        self.assertEqual(removed, [])
        self.assertIn("no unreferenced releases to prune", output.getvalue())
        self.assertFalse(os.path.lexists(lock_path))
        self.assertEqual(tree_snapshot(candidate_home), before)

    def test_dry_run_recovery_reports_but_does_not_mutate_transaction(
        self,
    ) -> None:
        self.install_pair()
        transaction = self.interrupt_before_commit()
        before = tree_snapshot(self.home)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            removed = MODULE.prune_releases(self.home, dry_run=True)

        self.assertEqual(removed, [])
        self.assertIn(
            "would recover release retention transaction",
            output.getvalue(),
        )
        self.assertEqual(tree_snapshot(self.home), before)
        self.assertTrue(transaction.batch_root.is_dir())
        self.assertTrue(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )

    def test_late_pin_after_commit_aborts_deletion_and_restores_release(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        real_publish = MODULE._publish_release_retention_commit_marker

        def publish_then_pin(
            home: Path,
            transaction: MODULE.ReleaseRetentionTransaction,
        ) -> None:
            real_publish(home, transaction)
            pin = MODULE._release_pin_path(
                home,
                MODULE.PUBLIC_OWNER,
                SHA_A,
            )
            MODULE._ensure_safe_internal_directory(
                home,
                pin.parent,
                create=True,
            )
            MODULE._write_exclusive_internal_file(
                home,
                pin,
                MODULE._release_pin_payload(MODULE.PUBLIC_OWNER, SHA_A),
            )

        with (
            mock.patch.object(
                MODULE,
                "_publish_release_retention_commit_marker",
                side_effect=publish_then_pin,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "became referenced before deletion",
            ),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)

        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))
        self.assertEqual(
            MODULE._release_pin_references(self.home),
            {(MODULE.PUBLIC_OWNER, SHA_A)},
        )

    def test_pointer_post_link_failure_preserves_recoverable_batch(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        real_publish = MODULE._publish_release_retention_pointer

        def publish_then_fail(
            home: Path,
            batch_root: Path,
            snapshot: MODULE.ManagedStateFileSnapshot,
        ) -> MODULE.ManagedStateFileSnapshot:
            real_publish(home, batch_root, snapshot)
            raise MODULE.SyncError("injected post-link failure")

        with (
            mock.patch.object(
                MODULE,
                "_publish_release_retention_pointer",
                side_effect=publish_then_fail,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "post-link failure"),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)

        transaction = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.batch_root.is_dir())
        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.run_quietly(
            MODULE._recover_release_retention_transaction,
            self.home,
            dry_run=False,
        )
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))
        self.assertEqual(self.release_identity(SHA_A), expected_identity)

    def test_interrupted_unpin_tombstone_remains_a_reference_and_recovers(
        self,
    ) -> None:
        self.install_pair()
        self.run_quietly(
            MODULE.pin_release,
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
        )
        pin = MODULE._release_pin_path(
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
        )
        retained_name = (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{pin.name}-123-0123456789abcdef"
        )

        def isolate_then_fail(
            home: Path,
            path: Path,
            parent_fd: int,
            expected: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            MODULE._rename_noreplace_at(
                parent_fd,
                path.name,
                parent_fd,
                retained_name,
            )
            os.fsync(parent_fd)
            raise MODULE.SyncError("injected unpin isolation failure")

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=isolate_then_fail,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "unpin isolation failure",
            ),
        ):
            self.run_quietly(
                MODULE.unpin_release,
                self.home,
                MODULE.PUBLIC_OWNER,
                SHA_A,
            )

        self.assertFalse(pin.exists())
        self.assertEqual(
            MODULE._release_pin_references(self.home),
            {(MODULE.PUBLIC_OWNER, SHA_A)},
        )
        self.run_quietly(
            MODULE.unpin_release,
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
        )
        self.assertFalse(pin.exists())
        self.assertEqual(MODULE._release_pin_references(self.home), set())

    def test_committed_double_missing_without_delete_start_is_ambiguous(
        self,
    ) -> None:
        self.install_pair()
        with (
            mock.patch.object(
                MODULE,
                "_publish_release_retention_delete_started_marker",
                side_effect=MODULE.SyncError("injected pre-delete-start failure"),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pre-delete-start failure",
            ),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)
        transaction = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.committed)
        self.assertFalse(transaction.deletion_started)
        shutil.rmtree(transaction.destination)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "deletion is ambiguous",
        ):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=True,
            )

        self.assertTrue(
            os.path.lexists(MODULE._release_retention_pointer_path(self.home))
        )
        self.assertTrue(transaction.batch_root.is_dir())

    def test_delete_start_closes_post_delete_pre_complete_crash_window(
        self,
    ) -> None:
        self.install_pair()
        with (
            mock.patch.object(
                MODULE,
                "_publish_release_retention_delete_complete_marker",
                side_effect=MODULE.SyncError(
                    "injected delete-complete publication failure"
                ),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "delete-complete publication failure",
            ),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)

        transaction = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.committed)
        self.assertTrue(transaction.deletion_started)
        self.assertFalse(transaction.deletion_complete)
        self.assertIsNone(MODULE._quarantined_release_path(self.home, transaction))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            removed = MODULE.prune_releases(self.home, dry_run=False)

        self.assertEqual(removed, [(MODULE.PUBLIC_OWNER, SHA_A)])
        self.assertIn("recovering durable quarantine", output.getvalue())
        self.assertNotIn("no unreferenced releases", output.getvalue())
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))

    def test_retained_final_clear_marker_is_recovered(
        self,
    ) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        self.interrupt_before_move()
        clear_marker = MODULE._release_retention_clear_marker_path(self.home)
        real_delete = MODULE._delete_retention_file
        injected = False

        def isolate_clear_then_fail(
            home: Path,
            path: Path,
            expected: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            nonlocal injected
            if path == clear_marker and not injected:
                injected = True
                parent_fd = MODULE._open_directory_beneath(home, path.parent)
                try:
                    retained_name = (
                        f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{path.name}-"
                        "123-0123456789abcdef"
                    )
                    MODULE._rename_noreplace_at(
                        parent_fd,
                        path.name,
                        parent_fd,
                        retained_name,
                    )
                    os.fsync(parent_fd)
                finally:
                    MODULE._close_fd_quietly(parent_fd)
                raise MODULE.SyncError("injected final-marker isolation failure")
            real_delete(home, path, expected, label=label)

        with (
            mock.patch.object(
                MODULE,
                "_delete_retention_file",
                side_effect=isolate_clear_then_fail,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "final-marker isolation failure",
            ),
        ):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=False,
            )

        clearing = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(clearing)
        assert clearing is not None
        self.assertTrue(clearing.clearing)
        self.assertIsNone(clearing.batch_root_identity)
        MODULE._recover_release_retention_transaction(
            self.home,
            dry_run=False,
        )
        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))

    def test_invalid_release_entries_are_bounded_before_candidate_filtering(
        self,
    ) -> None:
        self.install_pair()
        releases_root = MODULE._releases_root(
            self.home,
            MODULE.PUBLIC_OWNER,
        )
        for index in range(MODULE.MAX_RELEASE_RETENTION_CANDIDATES):
            (releases_root / f"invalid-{index:04d}").touch()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "release directory entry count exceeds the limit",
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=True)

        self.assertTrue(self.release_path(SHA_A).is_dir())
        self.assertTrue(self.release_path(SHA_B).is_dir())

    def test_recovery_rescans_references_at_delete_boundary(self) -> None:
        self.install_pair()
        expected_identity = self.release_identity(SHA_A)
        transaction = self.interrupt_after_commit()
        self.assertTrue(transaction.deletion_started)
        real_references = MODULE._retained_release_references
        scans = 0

        def pin_on_final_scan(
            home: Path,
            *,
            exclude_retention: tuple[str, str] | None = None,
        ):
            nonlocal scans
            scans += 1
            if scans == 2:
                pin = MODULE._release_pin_path(
                    home,
                    MODULE.PUBLIC_OWNER,
                    SHA_A,
                )
                MODULE._ensure_safe_internal_directory(
                    home,
                    pin.parent,
                    create=True,
                )
                MODULE._write_exclusive_internal_file(
                    home,
                    pin,
                    MODULE._release_pin_payload(
                        MODULE.PUBLIC_OWNER,
                        SHA_A,
                    ),
                )
            return real_references(
                home,
                exclude_retention=exclude_retention,
            )

        with (
            mock.patch.object(
                MODULE,
                "_retained_release_references",
                side_effect=pin_on_final_scan,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "referenced at recovery deletion",
            ),
        ):
            MODULE._recover_release_retention_transaction(
                self.home,
                dry_run=False,
            )

        self.assertGreaterEqual(scans, 2)
        self.assertEqual(self.release_identity(SHA_A), expected_identity)
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))
        self.assertEqual(
            MODULE._release_pin_references(self.home),
            {(MODULE.PUBLIC_OWNER, SHA_A)},
        )

    def test_deleted_clear_marker_preserves_recovery_reporting_outcome(
        self,
    ) -> None:
        self.install_pair()
        real_delete = MODULE._delete_retention_file
        injected = False

        def fail_before_commit_marker_cleanup(
            home: Path,
            path: Path,
            expected: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            nonlocal injected
            if (
                path.name == MODULE.RELEASE_RETENTION_COMMIT_MARKER_NAME
                and not injected
            ):
                injected = True
                raise MODULE.SyncError("injected commit-marker cleanup failure")
            real_delete(home, path, expected, label=label)

        with (
            mock.patch.object(
                MODULE,
                "_delete_retention_file",
                side_effect=fail_before_commit_marker_cleanup,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "commit-marker cleanup failure",
            ),
        ):
            self.run_quietly(MODULE.prune_releases, self.home, dry_run=False)

        clearing = MODULE._load_release_retention_transaction(self.home)
        self.assertIsNotNone(clearing)
        assert clearing is not None
        self.assertTrue(clearing.clearing)
        self.assertTrue(clearing.clearing_deleted)
        self.assertFalse(clearing.deletion_started)
        self.assertFalse(clearing.deletion_complete)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            removed = MODULE.prune_releases(self.home, dry_run=False)

        self.assertEqual(removed, [(MODULE.PUBLIC_OWNER, SHA_A)])
        self.assertIn("recovering durable quarantine", output.getvalue())
        self.assertNotIn("no unreferenced releases", output.getvalue())
        self.assertIsNone(MODULE._load_release_retention_transaction(self.home))


if __name__ == "__main__":
    unittest.main()
