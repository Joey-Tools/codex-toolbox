from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_regular_overlay_uninstall_status_regressions",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA_A = "a" * 40
SHA_B = "b" * 40
ROLE_TARGET = PurePosixPath("agents/reviewer.toml")
PUBLIC_PAYLOAD = 'provider = "public"\n'
PRIVATE_PAYLOAD = 'provider = "private"\n'


def write_regular_release(
    root: Path,
    *,
    owner: str = MODULE.PUBLIC_OWNER,
    payload: str,
    override: bool = False,
    base_sha: str | None = None,
) -> None:
    source = root / "personal_codex" / "agents" / "reviewer.toml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(payload, encoding="utf-8")
    entry: dict[str, object] = {
        "source": "personal_codex/agents/reviewer.toml",
        "target": ROLE_TARGET.as_posix(),
        "kind": "file",
        "owner": owner,
    }
    if override:
        entry["override"] = True
    manifest_payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": [entry],
    }
    if base_sha is not None:
        manifest_payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_sha,
        }
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_payload) + "\n", encoding="utf-8")


def write_non_regular_release(
    root: Path,
    *,
    owner: str = MODULE.PUBLIC_OWNER,
    base_sha: str | None = None,
) -> None:
    source = root / "personal_codex" / "config" / "keep.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("keep\n", encoding="utf-8")
    manifest_payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": [
            {
                "source": "personal_codex/config/keep.txt",
                "target": "config/keep.txt",
                "kind": "file",
                "owner": owner,
            }
        ],
    }
    if base_sha is not None:
        manifest_payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_sha,
        }
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_payload) + "\n", encoding="utf-8")


def install(root: Path, home: Path, sha: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, sha, dry_run=False)


def pending_authority_snapshot(home: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    roots = (
        MODULE._pending_link_pointer_path(home),
        MODULE._personal_sync_root(home) / MODULE.QUARANTINE_RELATIVE_PATH,
        MODULE._pending_cleanup_index_path(home),
    )
    for root in roots:
        if not os.path.lexists(root):
            continue
        paths = [root]
        if root.is_dir() and not root.is_symlink():
            paths.extend(root.rglob("*"))
        for path in paths:
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                payload: object = path.read_bytes()
            elif stat.S_ISLNK(metadata.st_mode):
                payload = os.readlink(path)
            else:
                payload = None
            snapshot[path.relative_to(home).as_posix()] = (
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                payload,
            )
    return snapshot


class RegularOverlayUninstallFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.public = self.root / "public"
        self.private = self.root / "private"
        write_regular_release(self.public, payload=PUBLIC_PAYLOAD)
        write_regular_release(
            self.private,
            owner="private",
            payload=PRIVATE_PAYLOAD,
            override=True,
            base_sha=SHA_A,
        )
        install(self.public, self.home, SHA_A)
        install(self.private, self.home, SHA_B)
        self.target = self.home / ROLE_TARGET

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_precommit_rollback_uses_regular_batch_finalizer(self) -> None:
        real_finalizer = MODULE._finalize_rolled_back_pending_batch
        real_verify_releases = MODULE._verify_install_release_identities
        failed = False

        def fail_after_pending_publication(
            home: Path,
            bindings,
            *,
            phase: str,
            verify_current: bool,
        ) -> None:
            nonlocal failed
            if phase == "before overlay uninstall" and not failed:
                failed = True
                raise MODULE.SyncError("injected precommit failure")
            real_verify_releases(
                home,
                bindings,
                phase=phase,
                verify_current=verify_current,
            )

        with (
            mock.patch.object(
                MODULE,
                "_verify_install_release_identities",
                side_effect=fail_after_pending_publication,
            ),
            mock.patch.object(
                MODULE,
                "_finalize_rolled_back_pending_batch",
                wraps=real_finalizer,
            ) as finalizer,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected precommit failure",
            ) as raised,
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(finalizer.call_count, 1, str(raised.exception))
        self.assertEqual(self.target.read_text(encoding="utf-8"), PRIVATE_PAYLOAD)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners["private"], SHA_B)
        self.assertEqual(state.links[ROLE_TARGET].owner, "private")

    def test_committed_cleanup_deferral_fails_closed_and_retries(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )
        self.assertGreater(self.target.stat().st_nlink, 1)
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertTrue(any(ticket_root.glob("*.json")))

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(self.target.read_text(encoding="utf-8"), PUBLIC_PAYLOAD)
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_A},
        )

    def test_terminal_cleanup_retains_batch_when_canonical_target_is_missing(
        self,
    ) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        ticket_paths = list(
            MODULE._pending_cleanup_index_path(self.home).glob("*.json")
        )
        self.assertEqual(len(ticket_paths), 1)
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_paths[0])
        self.assertIsNotNone(ticket)
        assert ticket is not None
        expected_identity = ticket.terminal_regular_targets[0].file_identity
        self.target.unlink()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "(managed regular file is unreadable|no source identity)",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        retained_identities = {
            (path.stat().st_dev, path.stat().st_ino)
            for path in ticket.batch_root.rglob("*")
            if path.is_file()
        }
        self.assertIn(expected_identity, retained_identities)
        self.assertTrue(ticket.path.is_file())
        self.assertFalse(
            MODULE._pending_cleanup_empty_proof_path(
                self.home,
                ticket.batch_root.name,
            ).exists()
        )

    def test_terminal_validation_receipt_survives_crash_before_deletion(
        self,
    ) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        ticket_path = next(
            MODULE._pending_cleanup_index_path(self.home).glob("*.json")
        )
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        receipt = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        proof = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        alias = ticket.batch_root / MODULE._pending_terminal_recovery_alias_name(0)
        self.assertTrue(receipt.is_file())
        self.assertFalse(proof.exists())
        self.assertTrue(alias.is_file())
        self.assertEqual(
            (alias.stat().st_dev, alias.stat().st_ino),
            ticket.terminal_regular_targets[0].file_identity,
        )

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.path.exists())
        self.assertFalse(receipt.exists())
        self.assertFalse(proof.exists())
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_cleanup_acl_query_failure_retains_terminal_batch(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        ticket_path = next(
            MODULE._pending_cleanup_index_path(self.home).glob("*.json")
        )
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        real_policy = MODULE._require_pending_cleanup_fd_access_policy

        def fail_batch_acl(file_descriptor, display_path, *, expected_mode):
            if display_path == ticket.batch_root:
                raise MODULE.SyncError("injected Darwin ACL query failure")
            return real_policy(
                file_descriptor,
                display_path,
                expected_mode=expected_mode,
            )

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_fd_access_policy",
                side_effect=fail_batch_acl,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "ACL query failure"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertGreater(self.target.stat().st_nlink, 1)

    def test_final_regular_target_is_verified_after_cleanup(self) -> None:
        real_verify = MODULE._verify_final_regular_targets

        def tamper_then_verify(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            self.target.write_text("tampered = true\n", encoding="utf-8")
            real_verify(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=tamper_then_verify,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

    def test_terminal_ticket_survives_crash_before_group_validation(self) -> None:
        real_verify = MODULE._verify_final_regular_targets
        crashed = False

        def crash_once(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise SystemExit("injected crash before terminal validation")
            real_verify(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=crash_once,
            ),
            self.assertRaisesRegex(SystemExit, "injected crash"),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertEqual(len(list(ticket_root.glob("*.json"))), 1)
        self.assertEqual(len(list(ticket_root.glob("*.empty-proof"))), 1)
        self.assertEqual(self.target.stat().st_nlink, 1)

        install(self.public, self.home, SHA_A)

        self.assertFalse(list(ticket_root.glob("*.json")))
        self.assertFalse(list(ticket_root.glob("*.empty-proof")))

    def test_terminal_validation_rejects_byte_identical_new_inode(self) -> None:
        real_verify = MODULE._verify_final_regular_targets

        def replace_then_verify(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            payload = self.target.read_bytes()
            original_target_fd = os.open(self.target, os.O_RDONLY)
            try:
                self.target.unlink()
                self.target.write_bytes(payload)
                self.target.chmod(0o600)
            finally:
                os.close(original_target_fd)
            real_verify(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=replace_then_verify,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(
            len(list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))),
            1,
        )

    def test_terminal_validation_rejects_replaced_parent_with_same_inode(self) -> None:
        real_verify = MODULE._verify_final_regular_targets

        def replace_parent_then_verify(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            parent = self.target.parent
            retained_parent = parent.with_name("agents-retained")
            parent.rename(retained_parent)
            parent.mkdir(mode=0o700)
            os.link(retained_parent / self.target.name, self.target)
            (retained_parent / self.target.name).unlink()
            real_verify(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=replace_parent_then_verify,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_malformed_terminal_ticket_fails_closed_before_recovery(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=SystemExit("injected terminal validation crash"),
            ),
            self.assertRaisesRegex(SystemExit, "terminal validation crash"),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        tickets = list(ticket_root.glob("*.json"))
        self.assertEqual(len(tickets), 1)
        tickets[0].write_bytes(b"{\n")
        tickets[0].chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "could not be safely classified",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        self.assertTrue(tickets[0].is_file())
        self.assertEqual(len(list(ticket_root.glob("*.empty-proof"))), 1)

    def test_ticket_tombstone_is_restored_and_cleanup_retries(self) -> None:
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file
        tripped = False

        def retain_ticket_then_fail(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
        ) -> None:
            nonlocal tripped
            proof_root = MODULE._pending_cleanup_index_path(home)
            if (
                not tripped
                and path.name.endswith(MODULE.PENDING_CLEANUP_TICKET_SUFFIX)
                and not os.path.lexists(MODULE._pending_link_pointer_path(home))
                and bool(list(proof_root.glob("*.empty-proof")))
            ):
                retained = next(MODULE._retained_pending_cleanup_names(path))
                MODULE._rename_noreplace_at(
                    parent_fd,
                    path.name,
                    parent_fd,
                    retained,
                )
                os.fsync(parent_fd)
                tripped = True
                raise MODULE.SyncError("injected ticket tombstone crash")
            real_delete(home, path, parent_fd, expected, label=label)

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=retain_ticket_then_fail,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertTrue(list(ticket_root.glob(".retained-cleanup-*.json-*")))
        install(self.public, self.home, SHA_A)
        self.assertFalse(list(ticket_root.glob(".retained-cleanup-*")))
        self.assertFalse(list(ticket_root.glob("*.json")))
        self.assertFalse(list(ticket_root.glob("*.empty-proof")))

    def test_empty_proof_tombstone_is_reconciled_without_ticket(self) -> None:
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file
        tripped = False

        def retain_proof_then_fail(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
            mutation_revalidator=None,
        ) -> None:
            nonlocal tripped
            if not tripped and path.name.endswith(
                MODULE.PENDING_CLEANUP_EMPTY_PROOF_SUFFIX
            ):
                retained = next(MODULE._retained_pending_cleanup_names(path))
                MODULE._rename_noreplace_at(
                    parent_fd,
                    path.name,
                    parent_fd,
                    retained,
                )
                os.fsync(parent_fd)
                tripped = True
                raise MODULE.SyncError("injected empty-proof tombstone crash")
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
                mutation_revalidator=mutation_revalidator,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=retain_proof_then_fail,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertFalse(list(ticket_root.glob("*.json")))
        self.assertTrue(list(ticket_root.glob(".retained-cleanup-*.empty-proof-*")))
        install(self.public, self.home, SHA_A)
        self.assertFalse(list(ticket_root.glob(".retained-cleanup-*")))
        self.assertFalse(list(ticket_root.glob("*.empty-proof")))

    def test_active_pointer_status_and_uninstall_dry_run_are_read_only(self) -> None:
        real_verify_releases = MODULE._verify_install_release_identities
        failed = False

        def fail_after_pending_publication(
            home: Path,
            bindings,
            *,
            phase: str,
            verify_current: bool,
        ) -> None:
            nonlocal failed
            if phase == "before overlay uninstall" and not failed:
                failed = True
                raise MODULE.SyncError("injected active pointer retention")
            real_verify_releases(
                home,
                bindings,
                phase=phase,
                verify_current=verify_current,
            )

        with (
            mock.patch.object(
                MODULE,
                "_verify_install_release_identities",
                side_effect=fail_after_pending_publication,
            ),
            mock.patch.object(
                MODULE,
                "_finalize_rolled_back_pending_batch",
                side_effect=MODULE.SyncError("retain active pointer"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        pointer = MODULE._pending_link_pointer_path(self.home)
        self.assertTrue(pointer.is_file())
        before = pending_authority_snapshot(self.home)
        status_output = io.StringIO()
        with contextlib.redirect_stdout(status_output):
            status_code = MODULE.main(
                ["status", "--home", str(self.home), "--strict"]
            )
        dry_run_output = io.StringIO()
        with contextlib.redirect_stdout(dry_run_output):
            MODULE.uninstall_overlay(self.home, "private", dry_run=True)

        after = pending_authority_snapshot(self.home)
        self.assertEqual(status_code, 1)
        self.assertIn(
            "active pending transaction must be recovered",
            status_output.getvalue(),
        )
        self.assertEqual(
            dry_run_output.getvalue().strip(),
            "would recover pending personal sync transaction under the install lock",
        )
        self.assertEqual(before, after)


class RegularPendingAuthorityReadOnlyTests(unittest.TestCase):
    def _installed_overlay_home(self, root: Path) -> Path:
        home = root / "home"
        public = root / "public"
        private = root / "private"
        write_regular_release(public, payload=PUBLIC_PAYLOAD)
        write_regular_release(
            private,
            owner="private",
            payload=PRIVATE_PAYLOAD,
            override=True,
            base_sha=SHA_A,
        )
        install(public, home, SHA_A)
        install(private, home, SHA_B)
        return home

    def _publish_pointerless_authority(self, home: Path, kind: str) -> Path:
        batch_root = MODULE._quarantine_batch_root(home, [])
        batch_identity = (batch_root.stat().st_dev, batch_root.stat().st_ino)
        if kind in {"v3", "retained"}:
            marker_path = batch_root / Path(
                *MODULE.PENDING_STATE_STAGING_MARKER.parts
            )
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            ticket = MODULE._mark_pending_batch_staging_cleanup_ready(
                home,
                batch_root,
                batch_identity,
            )
            authority = ticket.path
            if kind == "retained":
                retained_name = next(
                    MODULE._retained_pending_cleanup_names(authority)
                )
                retained = authority.with_name(retained_name)
                authority.rename(retained)
                authority = retained
            return authority
        self.assertEqual(kind, "v4")
        marker_path = batch_root / Path(*MODULE.PENDING_STATE_COMMIT_MARKER.parts)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker = MODULE._write_exclusive_internal_file(
            home,
            marker_path,
            b"legacy terminal cleanup marker\n",
        )
        self.assertIsNotNone(marker.parent_identity)
        self.assertIsNotNone(marker.file_identity)
        assert marker.parent_identity is not None
        assert marker.file_identity is not None
        payload = MODULE._bounded_json_document(
            {
                "version": 4,
                "batch": batch_root.name,
                "batch_root_identity": MODULE._identity_payload(batch_identity),
                "finalization_marker": {
                    "phase": "after",
                    "path": MODULE.PENDING_STATE_COMMIT_MARKER.as_posix(),
                    "parent_identity": MODULE._identity_payload(
                        marker.parent_identity
                    ),
                    "file_identity": MODULE._identity_payload(marker.file_identity),
                    "mode": 0o600,
                    "sha256": hashlib.sha256(marker.payload or b"").hexdigest(),
                },
                "terminal_regular_targets": [],
            },
            max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup ticket exceeds the size limit",
        )
        index_fd = MODULE._open_or_create_directory_beneath(
            home,
            MODULE._pending_cleanup_index_path(home),
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        authority = MODULE._pending_cleanup_ticket_path(home, batch_root.name)
        MODULE._publish_pending_cleanup_ticket(home, authority, payload)
        return authority

    def test_pointerless_authority_status_and_dry_run_leave_evidence_unchanged(
        self,
    ) -> None:
        for kind in ("v3", "v4", "retained"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                home = self._installed_overlay_home(Path(temporary))
                authority = self._publish_pointerless_authority(home, kind)
                self.assertTrue(authority.is_file())
                before = pending_authority_snapshot(home)

                status_output = io.StringIO()
                with contextlib.redirect_stdout(status_output):
                    status_code = MODULE.main(
                        ["status", "--home", str(home), "--strict"]
                    )
                dry_run_output = io.StringIO()
                with contextlib.redirect_stdout(dry_run_output):
                    MODULE.uninstall_overlay(home, "private", dry_run=True)

                after = pending_authority_snapshot(home)
                self.assertEqual(status_code, 1)
                self.assertIn(
                    "finalized or interrupted pending transaction must be cleaned",
                    status_output.getvalue(),
                )
                self.assertEqual(
                    dry_run_output.getvalue().strip(),
                    "would clean a finalized or interrupted pending transaction "
                    "under the install lock",
                )
                self.assertNotIn("would remove", dry_run_output.getvalue())
                self.assertNotIn("would replace", dry_run_output.getvalue())
                self.assertEqual(before, after)


class RegularStatusLedgerTests(unittest.TestCase):
    def test_public_status_accepts_and_validates_private_regular_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            public = root / "public"
            private = root / "private"
            write_regular_release(public, payload=PUBLIC_PAYLOAD)
            write_regular_release(
                private,
                owner="private",
                payload=PRIVATE_PAYLOAD,
                override=True,
                base_sha=SHA_A,
            )
            install(public, home, SHA_A)
            install(private, home, SHA_B)

            with contextlib.redirect_stdout(io.StringIO()):
                healthy = MODULE.status(home)

            self.assertTrue(healthy)

    def test_public_status_reports_stale_overlay_owner_without_current_pointer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            public = root / "public"
            private = root / "private"
            write_regular_release(public, payload=PUBLIC_PAYLOAD)
            write_regular_release(
                private,
                owner="private",
                payload=PUBLIC_PAYLOAD,
                override=True,
                base_sha=SHA_A,
            )
            install(public, home, SHA_A)
            install(private, home, SHA_B)
            MODULE._current_link(home, "private").unlink()
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                healthy = MODULE.status(home)

            self.assertFalse(healthy)
            self.assertIn(
                f"release mismatch: owner=private, state={SHA_B}, current=None",
                output.getvalue(),
            )

    def test_regular_replacement_verifier_requires_exact_transaction_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            release = root / "release"
            next_release = root / "next-release"
            write_regular_release(release, payload=PUBLIC_PAYLOAD)
            write_regular_release(next_release, payload='provider = "next"\n')
            install(release, home, SHA_A)
            next_manifest = MODULE.load_manifest_data(next_release)
            binding = MODULE._stage_release_tree_for_install(
                next_release,
                home,
                SHA_B,
                next_manifest,
            )
            MODULE._close_install_release_bindings([binding])
            state, state_snapshot = MODULE._load_managed_state_with_snapshot(home)
            current_manifest = MODULE._current_manifest_data(
                home,
                MODULE.PUBLIC_OWNER,
            )
            entry = current_manifest.entries[0]
            next_entry = next_manifest.entries[0]
            target = home / ROLE_TARGET
            next_source = (
                MODULE._releases_root(home, MODULE.PUBLIC_OWNER)
                / SHA_B
                / Path(*next_entry.source.parts)
            )
            actions = [
                MODULE.ReconcileAction(
                    "replace",
                    target,
                    MODULE._desired_link_target(home, next_entry),
                    next_entry.kind,
                    expected_link_target=MODULE._desired_link_target(home, entry),
                    planned_snapshot=MODULE._capture_reconcile_target_snapshot(
                        home,
                        target,
                    ),
                    materialization="regular",
                    regular_source=next_source,
                    regular_size=next_source.stat().st_size,
                )
            ]
            next_state = MODULE._planned_committed_state(
                home,
                next_manifest.entries,
                {MODULE.PUBLIC_OWNER: SHA_B},
                {ROLE_TARGET},
            )
            current_action = MODULE._plan_current_switch_action(
                home,
                SHA_B,
                MODULE.PUBLIC_OWNER,
            )
            self.assertIsNotNone(current_action)
            assert current_action is not None
            batch = MODULE._stage_pending_link_batch(
                home,
                [("current", [current_action]), ("managed", actions)],
                next_manifest.entries,
                {MODULE.PUBLIC_OWNER: SHA_B},
                state_snapshot,
                state,
                next_state,
            )

            with self.assertRaisesRegex(
                MODULE.SyncError,
                "active replacement target changed before removal",
            ):
                MODULE._verify_required_replacement_targets(home, [entry])

            MODULE._verify_required_replacement_targets(
                home,
                [entry],
                pending_batch=batch,
            )

            foreign_alias = root / "reviewer-alias"
            os.link(target, foreign_alias)
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "active replacement target changed before removal",
            ):
                MODULE._verify_required_replacement_targets(
                    home,
                    [entry],
                    pending_batch=batch,
                )

    def test_status_rejects_missing_mandatory_regular_state_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            release = root / "release"
            write_regular_release(release, payload=PUBLIC_PAYLOAD)
            install(release, home, SHA_A)
            state = MODULE._load_managed_state(home)
            state.links.pop(ROLE_TARGET)
            MODULE._write_managed_state(home, state)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                healthy = MODULE.status(home)

            self.assertFalse(healthy)
            self.assertIn(
                "regular file is missing its managed state claim",
                output.getvalue(),
            )

    def test_identical_override_rejects_legal_loser_claim_for_both_statuses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            public = root / "public"
            private = root / "private"
            write_regular_release(public, payload=PUBLIC_PAYLOAD)
            write_regular_release(
                private,
                owner="private",
                payload=PUBLIC_PAYLOAD,
                override=True,
                base_sha=SHA_A,
            )
            install(public, home, SHA_A)
            install(private, home, SHA_B)

            public_entry = MODULE._current_manifest_data(
                home,
                MODULE.PUBLIC_OWNER,
            ).entries[0]
            public_record = MODULE.ManagedLinkRecord(
                source=public_entry.source,
                target=public_entry.target,
                kind=public_entry.kind,
                owner=MODULE.PUBLIC_OWNER,
                link_target=MODULE._desired_link_target(home, public_entry),
                release_sha=SHA_A,
            )
            state = MODULE._load_managed_state(home)
            state.links[ROLE_TARGET] = public_record
            MODULE._write_managed_state(home, state)
            self.assertEqual(
                MODULE._load_managed_state(home).links[ROLE_TARGET],
                public_record,
            )

            public_output = io.StringIO()
            with contextlib.redirect_stdout(public_output):
                public_healthy = MODULE.status(home)
            private_output = io.StringIO()
            with contextlib.redirect_stdout(private_output):
                private_healthy = MODULE.status(home, "private")

            self.assertFalse(public_healthy)
            self.assertFalse(private_healthy)
            self.assertIn("winner claim conflict", public_output.getvalue())
            self.assertIn("winner claim conflict", private_output.getvalue())


class RegularClaimFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.state_path = MODULE._state_path(self.home)
        self.target = self.home / ROLE_TARGET

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _current_snapshot(self, owner: str) -> tuple[str, int, int]:
        current = MODULE._current_link(self.home, owner)
        metadata = current.lstat()
        return os.readlink(current), metadata.st_dev, metadata.st_ino

    def _target_snapshot(self) -> tuple[bytes, int, int, int, int, int]:
        metadata = self.target.stat()
        return (
            self.target.read_bytes(),
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_nlink,
        )

    def test_verify_overlay_rejects_missing_regular_claim_ledger(self) -> None:
        public = self.root / "public"
        private = self.root / "private"
        write_regular_release(public, payload=PUBLIC_PAYLOAD)
        write_regular_release(
            private,
            owner="private",
            payload=PRIVATE_PAYLOAD,
            override=True,
            base_sha=SHA_A,
        )
        install(public, self.home, SHA_A)
        install(private, self.home, SHA_B)
        self.state_path.unlink()
        output = io.StringIO()

        with (
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "overlay verification failed",
            ),
        ):
            MODULE.verify_overlay(self.home, "private")

        self.assertIn("missing overlay regular claim", output.getvalue())

    def test_same_release_install_does_not_bootstrap_exact_regular_bytes(
        self,
    ) -> None:
        public = self.root / "public"
        write_regular_release(public, payload=PUBLIC_PAYLOAD)
        install(public, self.home, SHA_A)
        current_before = self._current_snapshot(MODULE.PUBLIC_OWNER)
        target_before = self._target_snapshot()
        self.state_path.unlink()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unproven regular-file target",
        ):
            install(public, self.home, SHA_A)

        self.assertFalse(os.path.lexists(self.state_path))
        self.assertEqual(
            self._current_snapshot(MODULE.PUBLIC_OWNER),
            current_before,
        )
        self.assertEqual(self._target_snapshot(), target_before)

    def test_private_only_missing_regular_claim_blocks_uninstall(self) -> None:
        public = self.root / "public"
        private = self.root / "private"
        write_non_regular_release(public)
        write_regular_release(
            private,
            owner="private",
            payload=PRIVATE_PAYLOAD,
            base_sha=SHA_A,
        )
        install(public, self.home, SHA_A)
        install(private, self.home, SHA_B)
        state = MODULE._load_managed_state(self.home)
        state.links.pop(ROLE_TARGET)
        MODULE._write_managed_state(self.home, state)
        state_before = self.state_path.read_bytes()
        current_before = self._current_snapshot("private")
        target_before = self._target_snapshot()

        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "missing overlay regular claim",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(self.state_path.read_bytes(), state_before)
        self.assertEqual(self._current_snapshot("private"), current_before)
        self.assertEqual(self._target_snapshot(), target_before)
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners["private"],
            SHA_B,
        )

    def test_public_upgrade_cannot_retire_unclaimed_regular_target(self) -> None:
        public = self.root / "public"
        upgraded = self.root / "upgraded"
        write_regular_release(public, payload=PUBLIC_PAYLOAD)
        write_non_regular_release(upgraded)
        install(public, self.home, SHA_A)
        state = MODULE._load_managed_state(self.home)
        state.links.pop(ROLE_TARGET)
        MODULE._write_managed_state(self.home, state)
        state_before = self.state_path.read_bytes()
        current_before = self._current_snapshot(MODULE.PUBLIC_OWNER)
        target_before = self._target_snapshot()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "missing.*regular claim",
        ):
            install(upgraded, self.home, SHA_B)

        self.assertEqual(self.state_path.read_bytes(), state_before)
        self.assertEqual(
            self._current_snapshot(MODULE.PUBLIC_OWNER),
            current_before,
        )
        self.assertEqual(self._target_snapshot(), target_before)
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners[MODULE.PUBLIC_OWNER],
            SHA_A,
        )


if __name__ == "__main__":
    unittest.main()
