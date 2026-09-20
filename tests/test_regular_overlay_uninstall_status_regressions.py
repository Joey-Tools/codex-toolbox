from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
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

    def _deferred_terminal_ticket(self) -> MODULE.PendingBatchCleanupTicket:
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
        ticket_path = next(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        return ticket

    def _rewrite_ticket_same_inode(
        self,
        ticket: MODULE.PendingBatchCleanupTicket,
        payload: dict[str, object],
    ) -> None:
        encoded = MODULE._bounded_json_document(
            payload,
            max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup ticket exceeds the size limit",
        )
        with ticket.path.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        ticket.path.chmod(0o600)

    def _marker_only_v4_ticket(self) -> MODULE.PendingBatchCleanupTicket:
        ticket = self._deferred_terminal_ticket()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["version"] = MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION
        payload["terminal_regular_targets"] = []
        for field in (
            "commit_evidence",
            "terminal_namespace_sha256",
            "pointer_retirement_path",
            "pointer_retirement",
        ):
            payload.pop(field, None)
        self._rewrite_ticket_same_inode(ticket, payload)
        (ticket.batch_root / MODULE.PENDING_LINK_METADATA_NAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "created_at": "2026-09-20T00:00:00Z",
                    "actions": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(ticket.batch_root / "links")
        shutil.rmtree(ticket.batch_root / "state", ignore_errors=True)
        pending_root = ticket.batch_root / "pending"
        for child in pending_root.iterdir():
            if child.name != "state":
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        state_root = pending_root / "state"
        for child in state_root.iterdir():
            if child.name not in {"committed", "commit-evidence"}:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        legacy_ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
        self.assertIsNotNone(legacy_ticket)
        assert legacy_ticket is not None
        return legacy_ticket

    def _crash_after_walker_alias_unlink(
        self,
        ticket: MODULE.PendingBatchCleanupTicket,
        alias_parent: Path,
    ) -> None:
        expected_identity = ticket.terminal_regular_targets[0].file_identity
        alias_parent_identity = (
            alias_parent.stat().st_dev,
            alias_parent.stat().st_ino,
        )
        real_unlink = os.unlink
        crashed = False

        def crash_after_exact_alias_unlink(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            dir_fd: int | None = None,
            **kwargs: object,
        ) -> None:
            nonlocal crashed
            identity = None
            parent_identity = None
            if dir_fd is not None:
                parent = os.fstat(dir_fd)
                parent_identity = (parent.st_dev, parent.st_ino)
                try:
                    current = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    pass
                else:
                    identity = (current.st_dev, current.st_ino)
            real_unlink(path, *args, dir_fd=dir_fd, **kwargs)
            if (
                not crashed
                and parent_identity == alias_parent_identity
                and identity == expected_identity
            ):
                crashed = True
                raise SystemExit("injected walker alias unlink crash")

        with (
            mock.patch.object(
                MODULE.os, "unlink", side_effect=crash_after_exact_alias_unlink
            ),
            self.assertRaisesRegex(SystemExit, "walker alias unlink crash"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)
        self.assertTrue(crashed)
        self.assertTrue(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).is_file()
        )

    def _crash_after_walker_alias_parent_rmdir(
        self,
        ticket: MODULE.PendingBatchCleanupTicket,
        alias_parent: Path,
    ) -> None:
        expected_identity = (alias_parent.stat().st_dev, alias_parent.stat().st_ino)
        real_rmdir = os.rmdir
        crashed = False

        def crash_after_exact_alias_parent_rmdir(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            dir_fd: int | None = None,
            **kwargs: object,
        ) -> None:
            nonlocal crashed
            identity = None
            if dir_fd is not None:
                try:
                    current = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    pass
                else:
                    identity = (current.st_dev, current.st_ino)
            real_rmdir(path, *args, dir_fd=dir_fd, **kwargs)
            if not crashed and identity == expected_identity:
                crashed = True
                raise SystemExit("injected walker alias parent rmdir crash")

        with (
            mock.patch.object(
                MODULE.os,
                "rmdir",
                side_effect=crash_after_exact_alias_parent_rmdir,
            ),
            self.assertRaisesRegex(SystemExit, "alias parent rmdir crash"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)
        self.assertTrue(crashed)
        self.assertTrue(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).is_file()
        )

    def _terminal_receipt_alias_path(
        self,
        ticket: MODULE.PendingBatchCleanupTicket,
        namespace: str,
    ) -> MODULE.PendingTerminalValidationAlias:
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        try:
            receipt = MODULE._read_pending_cleanup_terminal_validation(
                self.home,
                ticket,
                MODULE._directory_identity(quarantine_fd),
            )
            self.assertIsNotNone(receipt)
            assert receipt is not None
            authority = MODULE._parse_pending_terminal_validation_authority(
                self.home,
                ticket,
                MODULE._directory_identity(quarantine_fd),
                receipt,
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        self.assertIsNotNone(authority)
        assert authority is not None
        return next(
            alias
            for alias in authority.aliases
            if alias.path.parts[:2] == ("pending", namespace)
        )

    def _directory_with_identity(
        self,
        root: Path,
        identity: tuple[int, int],
    ) -> Path:
        for path in (root, *root.rglob("*")):
            metadata = path.lstat()
            if (
                stat.S_ISDIR(metadata.st_mode)
                and (
                    metadata.st_dev,
                    metadata.st_ino,
                )
                == identity
            ):
                return path
        self.fail(f"missing terminal receipt alias parent: {identity}")

    def _active_batch_child(
        self,
        ticket: MODULE.PendingBatchCleanupTicket,
        logical_name: str,
    ) -> Path:
        batch_identity = (
            ticket.batch_root.stat().st_dev,
            ticket.batch_root.stat().st_ino,
        )
        for child in ticket.batch_root.iterdir():
            binding = MODULE._pending_cleanup_active_entry_binding(
                child.name,
                batch_identity,
            )
            if binding is not None and binding[1] == logical_name:
                return child
        self.fail(f"missing active batch child: {logical_name}")

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
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
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

        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
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

        ticket_path = next(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
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

    def test_rootless_retirement_rejects_incomplete_terminal_receipt(self) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_retire_terminal_regular_cleanup_controls",
                side_effect=SystemExit("injected before terminal retirement"),
            ),
            self.assertRaisesRegex(SystemExit, "before terminal retirement"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertFalse(ticket.batch_root.exists())
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        receipt_path.unlink()
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            receipt_path,
            MODULE._pending_cleanup_terminal_validation_payload(
                ticket,
                quarantine_identity,
            ),
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "receipt lacks complete retirement authority; manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(receipt_path.is_file())
        self.assertTrue(proof_path.is_file())

    def test_rootless_retirement_rejects_missing_terminal_receipt(self) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_retire_terminal_regular_cleanup_controls",
                side_effect=SystemExit("injected before terminal retirement"),
            ),
            self.assertRaisesRegex(SystemExit, "before terminal retirement"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        receipt_path.unlink()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "receipt is missing before control retirement; manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertFalse(receipt_path.exists())
        self.assertTrue(proof_path.is_file())

    def test_v8_terminal_validation_rejects_recreated_nonterminal_entry(self) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        nonterminal = ticket.batch_root / Path(*MODULE.PENDING_STATE_COMMIT_MARKER.parts)
        self.assertTrue(nonterminal.is_file())
        nonterminal.unlink()
        nonterminal.write_text("foreign replacement\n", encoding="utf-8")
        nonterminal.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal validation namespace changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertEqual(nonterminal.read_text(encoding="utf-8"), "foreign replacement\n")

    def test_v8_terminal_validation_rejects_same_inode_marker_rewrite(self) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        marker = ticket.batch_root / Path(*MODULE.PENDING_STATE_COMMIT_MARKER.parts)
        original = marker.read_bytes()
        with marker.open("r+b") as stream:
            stream.seek(0)
            stream.write(bytes([original[0] ^ 1]) + original[1:])
            stream.flush()
            os.fsync(stream.fileno())

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal validation namespace authority changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())

    def test_v8_terminal_validation_rejects_external_marker_hardlink(self) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        marker = ticket.batch_root / Path(*MODULE.PENDING_STATE_COMMIT_MARKER.parts)
        foreign = self.root / "foreign-finalization-marker"
        os.link(marker, foreign)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal validation namespace authority changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(foreign.is_file())

    def test_v8_terminal_validation_rejects_replaced_commit_evidence_before_receipt(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        evidence = ticket.batch_root / Path(*MODULE.PENDING_STATE_COMMIT_EVIDENCE.parts)
        evidence.unlink()
        evidence.write_bytes(b"foreign commit evidence\n")
        evidence.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "commit evidence and marker authority are split",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertEqual(evidence.read_bytes(), b"foreign commit evidence\n")

    def test_v8_terminal_validation_rejects_self_consistent_forged_namespace(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        quarantine_root = ticket.batch_root.parent
        quarantine_identity = (
            quarantine_root.stat().st_dev,
            quarantine_root.stat().st_ino,
        )
        receipt = MODULE._read_pending_cleanup_terminal_validation(
            self.home,
            ticket,
            quarantine_identity,
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        authority = MODULE._parse_pending_terminal_validation_authority(
            self.home,
            ticket,
            quarantine_identity,
            receipt,
        )
        self.assertIsNotNone(authority)
        assert authority is not None
        alias_paths = {alias.path for alias in authority.aliases}
        candidate = next(
            entry
            for entry in authority.namespace_entries or ()
            if entry.plan[2] == stat.S_IFREG
            and entry.path not in alias_paths
            and entry.path != PurePosixPath(MODULE.PENDING_LINK_METADATA_NAME)
            and entry.path.parts[:2] != ("pending", "state")
            and (not entry.path.parts or entry.path.parts[0] != "state")
        )
        candidate_path = ticket.batch_root / Path(*candidate.path.parts)
        candidate_path.unlink()
        candidate_path.write_text("forged namespace object\n", encoding="utf-8")
        candidate_path.chmod(0o600)
        replacement = candidate_path.stat()
        forged_entries = tuple(
            MODULE.replace(
                entry,
                plan=(replacement.st_dev, replacement.st_ino, stat.S_IFREG),
                mode=stat.S_IMODE(replacement.st_mode),
                uid=replacement.st_uid,
                gid=replacement.st_gid,
            )
            if entry.path == candidate.path
            else entry
            for entry in authority.namespace_entries or ()
        )
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        receipt_path.unlink()
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            receipt_path,
            MODULE._pending_cleanup_terminal_validation_payload(
                ticket,
                quarantine_identity,
                terminal_aliases=authority.aliases,
                terminal_directories=authority.directories,
                namespace_entries=forged_entries,
            ),
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal validation namespace authority changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertEqual(
            candidate_path.read_text(encoding="utf-8"),
            "forged namespace object\n",
        )

    def test_v8_terminal_validation_rejects_incomplete_forged_control_set(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        quarantine_root = ticket.batch_root.parent
        quarantine_identity = (
            quarantine_root.stat().st_dev,
            quarantine_root.stat().st_ino,
        )
        receipt = MODULE._read_pending_cleanup_terminal_validation(
            self.home,
            ticket,
            quarantine_identity,
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        authority = MODULE._parse_pending_terminal_validation_authority(
            self.home,
            ticket,
            quarantine_identity,
            receipt,
        )
        self.assertIsNotNone(authority)
        assert authority is not None
        forged_controls = tuple(
            MODULE.replace(
                control,
                paths=tuple(
                    path for path in control.paths if path.path != ticket.marker_path
                ),
                link_count=sum(
                    path.path != ticket.marker_path for path in control.paths
                ),
            )
            for control in authority.control_files
            if any(path.path != ticket.marker_path for path in control.paths)
        )
        self.assertTrue(forged_controls)
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        receipt_path.unlink()
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            receipt_path,
            MODULE._pending_cleanup_terminal_validation_payload(
                ticket,
                quarantine_identity,
                terminal_aliases=authority.aliases,
                terminal_directories=authority.directories,
                namespace_entries=authority.namespace_entries,
                control_files=forged_controls,
            ),
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "control authority is incomplete",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())

    def test_v8_terminal_validation_rejects_forged_marker_digest(self) -> None:
        ticket = self._deferred_terminal_ticket()
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        quarantine_root = ticket.batch_root.parent
        quarantine_identity = (
            quarantine_root.stat().st_dev,
            quarantine_root.stat().st_ino,
        )
        receipt = MODULE._read_pending_cleanup_terminal_validation(
            self.home,
            ticket,
            quarantine_identity,
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        authority = MODULE._parse_pending_terminal_validation_authority(
            self.home,
            ticket,
            quarantine_identity,
            receipt,
        )
        self.assertIsNotNone(authority)
        assert authority is not None
        marker = ticket.batch_root / Path(*ticket.marker_path.parts)
        original = marker.read_bytes()
        replacement = bytes([original[0] ^ 1]) + original[1:]
        marker.write_bytes(replacement)
        marker.chmod(0o600)
        marker_controls = tuple(
            MODULE.replace(
                control,
                sha256=hashlib.sha256(replacement).hexdigest(),
            )
            if any(path.path == ticket.marker_path for path in control.paths)
            else control
            for control in authority.control_files
        )
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        receipt_path.unlink()
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            receipt_path,
            MODULE._pending_cleanup_terminal_validation_payload(
                ticket,
                quarantine_identity,
                terminal_aliases=authority.aliases,
                terminal_directories=authority.directories,
                namespace_entries=authority.namespace_entries,
                control_files=marker_controls,
            ),
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "marker authority changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertEqual(marker.read_bytes(), replacement)

    def test_terminal_receipt_capacity_preflight_includes_control_files(self) -> None:
        ticket = self._deferred_terminal_ticket()
        quarantine_root = ticket.batch_root.parent
        quarantine_identity = (
            quarantine_root.stat().st_dev,
            quarantine_root.stat().st_ino,
        )
        batch_fd = MODULE._open_directory_beneath(self.home, ticket.batch_root)
        try:
            with mock.patch.object(
                MODULE,
                "_pending_cleanup_terminal_validation_payload",
                wraps=MODULE._pending_cleanup_terminal_validation_payload,
            ) as payload:
                MODULE._validate_pending_terminal_validation_receipt_namespace_capacity(
                    self.home,
                    ticket,
                    ticket.batch_root,
                    batch_fd,
                    MODULE._directory_mount_identity(batch_fd),
                    quarantine_identity,
                )
        finally:
            MODULE._close_fd_quietly(batch_fd)
        self.assertTrue(payload.call_args.kwargs["control_files"])

    def test_v8_terminal_validation_allows_owner_only_gid_drift(self) -> None:
        ticket = self._deferred_terminal_ticket()
        real_capture = MODULE._capture_pending_cleanup_identity_ledger
        drifted_parents: set[tuple[int, int]] = set()

        def capture_with_benign_gid_drift(*args: object, **kwargs: object) -> None:
            real_capture(*args, **kwargs)
            ledger = args[4]
            assert isinstance(ledger, dict)
            for parent_identity, entries in tuple(ledger.items()):
                if parent_identity in drifted_parents:
                    continue
                for index, entry in enumerate(entries):
                    planned = entry[1]
                    mode = entry[7]
                    if (
                        planned[2] == stat.S_IFREG
                        and not bool(
                            mode & MODULE._REGULAR_FILE_GID_SENSITIVE_MODE_MASK
                        )
                    ):
                        updated = list(entry)
                        updated[9] += 1
                        replacement_entries = list(entries)
                        replacement_entries[index] = tuple(updated)
                        ledger[parent_identity] = tuple(replacement_entries)
                        drifted_parents.add(parent_identity)
                        return

        with mock.patch.object(
            MODULE,
            "_capture_pending_cleanup_identity_ledger",
            side_effect=capture_with_benign_gid_drift,
        ):
            self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

        self.assertTrue(drifted_parents)
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_v8_ticket_without_namespace_anchor_requires_manual_recovery(self) -> None:
        ticket = self._deferred_terminal_ticket()
        ticket_payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(ticket_payload, dict)
        assert isinstance(ticket_payload, dict)
        ticket_payload.pop("terminal_namespace_sha256")
        ticket.path.write_bytes(
            MODULE._bounded_json_document(
                ticket_payload,
                max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
                overflow_error="pending cleanup ticket exceeds the size limit",
            )
        )
        legacy_ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
        self.assertIsNotNone(legacy_ticket)
        assert legacy_ticket is not None
        foreign = ticket.batch_root / "links" / "foreign-entry"
        foreign.parent.mkdir(mode=0o700, exist_ok=True)
        foreign.write_bytes(b"foreign link content\n")
        foreign.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "namespace authority anchor.*manual recovery",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, legacy_ticket)

        self.assertTrue(legacy_ticket.path.is_file())
        self.assertTrue(legacy_ticket.batch_root.is_dir())
        self.assertFalse(
            (
                legacy_ticket.batch_root
                / MODULE._pending_terminal_recovery_alias_name(0)
            ).exists()
        )
        self.assertEqual(foreign.read_bytes(), b"foreign link content\n")

    def test_v8_ticket_without_pointer_path_rejects_late_state_lookalike(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        ticket_payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(ticket_payload, dict)
        assert isinstance(ticket_payload, dict)
        ticket_payload.pop("pointer_retirement_path")
        ticket_payload.pop("pointer_retirement")
        ticket.path.write_bytes(
            MODULE._bounded_json_document(
                ticket_payload,
                max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
                overflow_error="pending cleanup ticket exceeds the size limit",
            )
        )
        legacy_ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
        self.assertIsNotNone(legacy_ticket)
        assert legacy_ticket is not None

        lookalike = (
            legacy_ticket.batch_root
            / "state"
            / "pending-complete-1-1"
        )
        lookalike.parent.mkdir(mode=0o700, exist_ok=True)
        lookalike.write_bytes(b"foreign state lookalike\n")
        lookalike.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "exact pointer retirement authority.*manual recovery",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, legacy_ticket)

        self.assertTrue(legacy_ticket.path.is_file())
        self.assertTrue(legacy_ticket.batch_root.is_dir())
        self.assertFalse(
            (
                legacy_ticket.batch_root
                / MODULE._pending_terminal_recovery_alias_name(0)
            ).exists()
        )
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                legacy_ticket.batch_root.name,
            ).exists()
        )
        self.assertEqual(lookalike.read_bytes(), b"foreign state lookalike\n")

    def test_v8_terminal_validation_rejects_replaced_pointer_retirement_before_receipt(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertIsNotNone(ticket.pointer_retirement_path)
        assert ticket.pointer_retirement_path is not None
        retirement = ticket.batch_root / Path(*ticket.pointer_retirement_path.parts)
        self.assertTrue(retirement.is_file())
        retirement.unlink()
        retirement.write_bytes(b"foreign pointer retirement\n")
        retirement.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pointer retirement object changed; manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).exists()
        )
        self.assertEqual(retirement.read_bytes(), b"foreign pointer retirement\n")

    def test_v8_terminal_validation_rejects_replaced_transaction_metadata_before_receipt(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        metadata = ticket.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        metadata.unlink()
        metadata.write_bytes(b"foreign transaction metadata\n")
        metadata.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending transaction metadata changed; manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).exists()
        )
        self.assertEqual(
            metadata.read_bytes(),
            b"foreign transaction metadata\n",
        )

    def test_v8_terminal_validation_rejects_one_sided_pointer_authority_before_receipt(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        metadata = ticket.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        metadata.unlink()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pointer retirement authority is incomplete; manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).exists()
        )

    def test_v8_terminal_validation_rejects_pointer_retirement_hardlink_alias(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertIsNotNone(ticket.pointer_retirement_path)
        assert ticket.pointer_retirement_path is not None
        retirement = ticket.batch_root / Path(*ticket.pointer_retirement_path.parts)
        foreign = self.root / "foreign-pointer-retirement.toml"
        os.link(retirement, foreign)

        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pointer retirement object changed; manual recovery is required",
            ):
                MODULE._remove_cleanup_ready_batch(self.home, ticket)

            self.assertTrue(ticket.path.is_file())
            self.assertTrue(ticket.batch_root.is_dir())
            self.assertTrue(foreign.is_file())
            self.assertFalse(
                MODULE._pending_cleanup_terminal_validation_path(
                    self.home,
                    ticket.batch_root.name,
                ).exists()
            )
        finally:
            foreign.unlink()

    def test_v8_pointer_retirement_fallback_uses_shared_entry_budget(self) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertIsNotNone(ticket.pointer_retirement_path)
        assert ticket.pointer_retirement_path is not None
        state_root = ticket.batch_root / Path(
            *ticket.pointer_retirement_path.parent.parts
        )
        shutil.rmtree(state_root)
        batch_fd = MODULE._open_directory_beneath(self.home, ticket.batch_root)
        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup control scan exceeds the entry budget",
            ):
                MODULE._require_pending_terminal_pointer_retirement_authority(
                    self.home,
                    ticket,
                    ticket.batch_root,
                    batch_fd,
                    allow_consumed=False,
                    entry_budget=[0],
                )
        finally:
            MODULE._close_fd_quietly(batch_fd)

    def test_v8_terminal_validation_rejects_foreign_alias_after_metadata_consumption(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertIsNotNone(ticket.pointer_retirement_path)
        assert ticket.pointer_retirement_path is not None
        retirement = ticket.batch_root / Path(*ticket.pointer_retirement_path.parts)
        metadata = ticket.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        foreign = self.root / "foreign-pointer-after-metadata-consumption.toml"

        def consume_metadata_then_crash(*args: object, **kwargs: object) -> None:
            metadata.unlink()
            os.link(retirement, foreign)
            raise SystemExit("injected post-metadata-consumption crash")

        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=consume_metadata_then_crash,
            ),
            self.assertRaisesRegex(SystemExit, "post-metadata-consumption crash"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).is_file()
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pointer retirement object changed; manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(foreign.is_file())

    def test_v8_terminal_validation_capacity_is_preflighted_before_alias_creation(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        alias = ticket.batch_root / MODULE._pending_terminal_recovery_alias_name(0)
        with (
            mock.patch.object(
                MODULE,
                "_pending_cleanup_terminal_validation_payload",
                side_effect=MODULE.SyncError(
                    "pending cleanup validation receipt exceeds the size limit"
                ),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup validation receipt exceeds the size limit",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertFalse(alias.exists())
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).exists()
        )

    def test_v8_terminal_validation_rechecks_ticket_before_hardlink(self) -> None:
        ticket = self._deferred_terminal_ticket()
        alias = ticket.batch_root / MODULE._pending_terminal_recovery_alias_name(0)
        real_publish = MODULE._publish_regular_hardlink_beneath
        rewritten = False

        def rewrite_ticket_before_publication(*args: object, **kwargs: object):
            nonlocal rewritten
            if not rewritten:
                payload = json.loads(ticket.path.read_text(encoding="utf-8"))
                self.assertIsInstance(payload, dict)
                assert isinstance(payload, dict)
                payload["terminal_namespace_sha256"] = "0" * 64
                ticket.path.write_bytes(
                    MODULE._bounded_json_document(
                        payload,
                        max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
                        overflow_error="pending cleanup ticket exceeds the size limit",
                    )
                )
                rewritten = True
            return real_publish(*args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_publish_regular_hardlink_beneath",
                side_effect=rewrite_ticket_before_publication,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket changed",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(rewritten)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertFalse(alias.exists())

    def test_v8_terminal_validation_rechecks_ticket_before_walker_mutation(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        real_remove = MODULE._remove_pending_batch_directory_contents
        rewritten = False

        def rewrite_ticket_before_walker(*args: object, **kwargs: object):
            nonlocal rewritten
            if not rewritten:
                payload = json.loads(ticket.path.read_text(encoding="utf-8"))
                self.assertIsInstance(payload, dict)
                assert isinstance(payload, dict)
                payload["terminal_namespace_sha256"] = "0" * 64
                ticket.path.write_bytes(
                    MODULE._bounded_json_document(
                        payload,
                        max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
                        overflow_error="pending cleanup ticket exceeds the size limit",
                    )
                )
                rewritten = True
            return real_remove(*args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=rewrite_ticket_before_walker,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket changed",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(rewritten)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())

    def test_v4_terminal_validation_rechecks_ticket_before_walker_mutation(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["version"] = MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION
        targets = payload.get("terminal_regular_targets")
        self.assertIsInstance(targets, list)
        assert isinstance(targets, list)
        for target in targets:
            self.assertIsInstance(target, dict)
            assert isinstance(target, dict)
            target.pop("link_count", None)
        payload.pop("terminal_namespace_sha256", None)
        payload.pop("pointer_retirement_path", None)
        payload.pop("pointer_retirement", None)
        ticket.path.write_bytes(
            MODULE._bounded_json_document(
                payload,
                max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
                overflow_error="pending cleanup ticket exceeds the size limit",
            )
        )
        legacy_ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
        self.assertIsNotNone(legacy_ticket)
        assert legacy_ticket is not None
        self.assertEqual(
            legacy_ticket.version,
            MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION,
        )
        alias = ticket.batch_root / MODULE._pending_terminal_recovery_alias_name(0)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "legacy terminal validation ticket lacks namespace authority; "
            "manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, legacy_ticket)

        self.assertTrue(legacy_ticket.path.is_file())
        self.assertTrue(legacy_ticket.batch_root.is_dir())
        self.assertFalse(alias.exists())

    def test_v4_terminal_validation_rejects_namespace_added_during_receipt_publish(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["version"] = MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION
        targets = payload.get("terminal_regular_targets")
        self.assertIsInstance(targets, list)
        assert isinstance(targets, list)
        for target in targets:
            self.assertIsInstance(target, dict)
            assert isinstance(target, dict)
            target.pop("link_count", None)
        payload.pop("terminal_namespace_sha256", None)
        payload.pop("pointer_retirement_path", None)
        payload.pop("pointer_retirement", None)
        ticket.path.write_bytes(
            MODULE._bounded_json_document(
                payload,
                max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
                overflow_error="pending cleanup ticket exceeds the size limit",
            )
        )
        legacy_ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
        self.assertIsNotNone(legacy_ticket)
        assert legacy_ticket is not None
        foreign = ticket.batch_root / "links" / "foreign-entry"
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "legacy terminal validation ticket lacks namespace authority; "
            "manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, legacy_ticket)

        self.assertTrue(legacy_ticket.path.is_file())
        self.assertTrue(legacy_ticket.batch_root.is_dir())
        self.assertFalse(foreign.exists())

    def test_v4_identity_ledger_match_allows_owner_only_gid_drift(self) -> None:
        common = (
            "entry",
            (11, 22, stat.S_IFREG),
            False,
            False,
            "entry",
            True,
            PurePosixPath("entry"),
            0o600,
            os.geteuid(),
        )
        left = {(1, 2): (common + (100,),)}
        right = {(1, 2): (common + (200,),)}

        self.assertTrue(MODULE._pending_cleanup_identity_ledgers_match(left, right))

    def test_v8_namespace_anchor_binds_state_retirement_lookalike(self) -> None:
        entry = MODULE.PendingTerminalValidationNamespaceEntry(
            path=PurePosixPath("state", "pending-complete-1-1"),
            parent_identity=(11, 22),
            plan=(33, 44, stat.S_IFREG),
            mode=0o600,
            uid=os.geteuid(),
            gid=os.getegid(),
        )
        pointer_retirement = MODULE.replace(
            entry,
            path=PurePosixPath(
                "state",
                "pending-complete-20260910T000000Z-1-2",
            ),
        )

        original = MODULE._pending_terminal_validation_namespace_anchor_digest(
            (entry, pointer_retirement),
            (),
            pointer_retirement_path=pointer_retirement.path,
        )
        retired_replacement = MODULE.replace(
            pointer_retirement,
            plan=(55, 66, stat.S_IFREG),
        )
        self.assertEqual(
            original,
            MODULE._pending_terminal_validation_namespace_anchor_digest(
                (entry, retired_replacement),
                (),
                pointer_retirement_path=pointer_retirement.path,
            ),
        )
        replacement = MODULE.replace(entry, plan=(55, 66, stat.S_IFREG))

        self.assertNotEqual(
            original,
            MODULE._pending_terminal_validation_namespace_anchor_digest(
                (replacement, pointer_retirement),
                (),
                pointer_retirement_path=pointer_retirement.path,
            ),
        )
        self.assertNotEqual(
            MODULE._pending_terminal_validation_namespace_anchor_digest_legacy(
                (),
                (),
            ),
            MODULE._pending_terminal_validation_namespace_anchor_digest_legacy(
                (entry,),
                (),
            ),
        )

    def test_terminal_receipt_capacity_projects_deep_backup_namespace(self) -> None:
        terminal_target = PurePosixPath("agents", "reviewer.toml")
        terminal_record = MODULE.ManagedLinkRecord(
            source=PurePosixPath("personal_codex", "agents", "reviewer.toml"),
            target=terminal_target,
            kind="file",
            owner=MODULE.PUBLIC_OWNER,
            link_target="release",
            release_sha=SHA_A,
        )
        terminal_snapshot = MODULE.ReconcileTargetSnapshot(
            parent_identity=(1, 2),
            link_identity=(3, 4),
            regular_sha256="a" * 64,
            regular_size=1,
            regular_mode=0o600,
            regular_uid=0,
            regular_gid=0,
            regular_link_count=1,
        )
        actions = [
            MODULE.ReconcileAction(
                "remove",
                self.home / Path(*terminal_target.parts),
                "release",
                "file",
                planned_snapshot=terminal_snapshot,
            )
        ]
        for index in range(1200):
            target = PurePosixPath(
                "deep",
                f"{index:04d}",
                *("segment",) * 62,
            )
            actions.append(
                MODULE.ReconcileAction(
                    "remove",
                    self.home / Path(*target.parts),
                    "release",
                    "skill",
                    planned_snapshot=MODULE.ReconcileTargetSnapshot(
                        parent_identity=(5, 6),
                        link_identity=(7, 8),
                        link_target="release",
                    ),
                )
            )
        capacity = MODULE.PendingLinkCapacityPlan(
            ordered_groups=(("managed", tuple(actions)),),
            flattened_actions=tuple(actions),
            retired_absence_specs=(),
        )
        before_state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: SHA_A},
            links={terminal_target: terminal_record},
        )
        after_state = MODULE.ManagedState(owners={}, links={})
        record_actions = {
            (
                "managed",
                PurePosixPath(*action.target.relative_to(self.home).parts),
            ): action.action
            for action in capacity.flattened_actions
        }
        namespace_paths = MODULE._projected_pending_terminal_validation_namespace_paths(
            self.home,
            capacity,
            before_state,
            before_state,
            after_state,
            record_actions,
            state_before_exists=False,
        )
        projected_aliases = MODULE._projected_pending_terminal_validation_alias_paths(
            self.home,
            capacity,
            before_state,
            phase="before",
        )
        self.assertTrue(set(projected_aliases).issubset(namespace_paths))
        self.assertNotIn(MODULE.PENDING_STATE_BEFORE_EVIDENCE, namespace_paths)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal validation receipt exceeds the size limit",
        ):
            MODULE._validate_pending_link_metadata_capacity(
                self.home,
                capacity,
                MODULE.ManagedStateFileSnapshot(exists=False),
                before_state,
                before_state,
                after_state,
            )

    def test_terminal_validation_resumes_after_stage_alias_is_consumed(self) -> None:
        ticket = self._deferred_terminal_ticket()
        self._crash_after_walker_alias_unlink(
            ticket,
            ticket.batch_root / "pending" / "stage",
        )

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_resumes_after_evidence_alias_is_consumed(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self._crash_after_walker_alias_unlink(
            ticket,
            ticket.batch_root / "pending" / "evidence",
        )

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def _assert_terminal_validation_rejects_foreign_reappearance(
        self,
        namespace: str,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        alias_parent = ticket.batch_root / "pending" / namespace
        self._crash_after_walker_alias_unlink(ticket, alias_parent)
        receipt_alias = self._terminal_receipt_alias_path(ticket, namespace)
        alias_parent = self._directory_with_identity(
            ticket.batch_root,
            receipt_alias.parent_identity,
        )
        alias = alias_parent / receipt_alias.path.name
        self.assertFalse(alias.exists())
        alias.write_text("foreign replacement\n", encoding="utf-8")
        alias.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal regular alias changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(alias.is_file())
        alias.unlink()
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_rejects_foreign_stage_alias_reappearance(
        self,
    ) -> None:
        self._assert_terminal_validation_rejects_foreign_reappearance("stage")

    def test_terminal_validation_rejects_foreign_evidence_alias_reappearance(
        self,
    ) -> None:
        self._assert_terminal_validation_rejects_foreign_reappearance("evidence")

    def _assert_terminal_validation_rejects_recreated_alias_parent(
        self,
        replacement: str,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        stage = ticket.batch_root / "pending" / "stage"
        self._crash_after_walker_alias_parent_rmdir(ticket, stage)
        pending = self._active_batch_child(ticket, "pending")
        foreign = pending / "stage"
        if replacement == "directory":
            foreign.mkdir(mode=0o700)
            foreign.chmod(0o700)
        else:
            foreign.symlink_to("foreign-replacement")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal regular alias namespace changed: pending/stage",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(os.path.lexists(foreign))
        if replacement == "directory":
            foreign.rmdir()
        else:
            foreign.unlink()
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_rejects_recreated_stage_directory_after_rmdir(
        self,
    ) -> None:
        self._assert_terminal_validation_rejects_recreated_alias_parent("directory")

    def test_terminal_validation_rejects_recreated_stage_symlink_after_rmdir(
        self,
    ) -> None:
        self._assert_terminal_validation_rejects_recreated_alias_parent("symlink")

    def test_terminal_validation_rejects_forged_v2_active_stage_after_rmdir(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        stage = ticket.batch_root / "pending" / "stage"
        self._crash_after_walker_alias_parent_rmdir(ticket, stage)
        pending = self._active_batch_child(ticket, "pending")
        pending_fd = MODULE._open_directory_beneath(self.home, pending)
        temporary_name = "stage-foreign-temporary"
        try:
            os.mkdir(temporary_name, 0o700, dir_fd=pending_fd)
            temporary = os.stat(
                temporary_name,
                dir_fd=pending_fd,
                follow_symlinks=False,
            )
            planned = MODULE._pending_cleanup_entry_plan(temporary)
            active_name = MODULE._pending_cleanup_active_entry_name(
                pending_fd,
                MODULE._directory_identity(pending_fd),
                planned,
                "stage",
            )
            MODULE._rename_noreplace_at(
                pending_fd,
                temporary_name,
                pending_fd,
                active_name,
            )
            os.fsync(pending_fd)
        finally:
            MODULE._close_fd_quietly(pending_fd)
        foreign_directory = pending / active_name
        foreign_file = foreign_directory / "99999999"
        foreign_file.write_text("foreign replacement\n", encoding="utf-8")
        foreign_file.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal regular alias namespace changed: pending/stage",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(foreign_file.is_file())
        foreign_file.unlink()
        foreign_directory.rmdir()
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_rejects_recreated_pending_after_rmdir(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        pending = ticket.batch_root / "pending"
        self._crash_after_walker_alias_parent_rmdir(ticket, pending)
        self.assertFalse(pending.exists())

        pending.mkdir(mode=0o700)
        pending.chmod(0o700)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal regular alias namespace changed: pending",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(pending.is_dir())
        pending.rmdir()
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_resumes_after_recovery_alias_is_consumed(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self._crash_after_walker_alias_unlink(ticket, ticket.batch_root)

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_resumes_legacy_active_regular_entry(self) -> None:
        ticket = self._deferred_terminal_ticket()
        stage = ticket.batch_root / "pending" / "stage"
        regular = next(path for path in stage.iterdir() if path.is_file())
        parent_fd = MODULE._open_directory_beneath(self.home, stage)
        try:
            parent_identity = MODULE._directory_identity(parent_fd)
            planned = MODULE._pending_cleanup_entry_plan(
                os.stat(regular.name, dir_fd=parent_fd, follow_symlinks=False)
            )
            legacy_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                parent_identity,
                planned,
            )
            self.assertGreater(
                len(os.fsencode(legacy_name)),
                MODULE.MAX_PENDING_CLEANUP_ACTIVE_LOGICAL_NAME_BYTES,
            )
            MODULE._rename_noreplace_at(
                parent_fd,
                regular.name,
                parent_fd,
                legacy_name,
            )
            os.fsync(parent_fd)
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_resumes_name_max_fallback_after_rename(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        expected_identities = {
            expectation.file_identity
            for expectation in ticket.terminal_regular_targets
        }
        stage = ticket.batch_root / "pending" / "stage"
        stage_alias = next(
            path
            for path in stage.iterdir()
            if (path.stat().st_dev, path.stat().st_ino) in expected_identities
        )
        links = ticket.batch_root / "links"
        logical_name = "x" * MODULE.MAX_PENDING_CLEANUP_ACTIVE_LOGICAL_NAME_BYTES
        stage_fd = MODULE._open_directory_beneath(self.home, stage)
        links_fd = MODULE._open_directory_beneath(self.home, links)
        try:
            MODULE._rename_noreplace_at(
                stage_fd,
                stage_alias.name,
                links_fd,
                logical_name,
            )
            os.fsync(stage_fd)
            os.fsync(links_fd)
        finally:
            MODULE._close_fd_quietly(links_fd)
            MODULE._close_fd_quietly(stage_fd)
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected crash after receipt"),
            ),
            self.assertRaisesRegex(SystemExit, "after receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        try:
            receipt = MODULE._read_pending_cleanup_terminal_validation(
                self.home,
                ticket,
                MODULE._directory_identity(quarantine_fd),
            )
            self.assertIsNotNone(receipt)
            assert receipt is not None
            authority = MODULE._parse_pending_terminal_validation_authority(
                self.home,
                ticket,
                MODULE._directory_identity(quarantine_fd),
                receipt,
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        self.assertIsNotNone(authority)
        assert authority is not None
        links_alias = next(
            alias
            for alias in authority.aliases
            if alias.path == PurePosixPath("links", logical_name)
        )

        real_unlink = os.unlink
        crashed = False
        fallback_name: str | None = None

        def crash_before_fallback_unlink(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            dir_fd: int | None = None,
            **kwargs: object,
        ) -> None:
            nonlocal crashed, fallback_name
            current_identity = None
            parent_identity = None
            if dir_fd is not None:
                parent = os.fstat(dir_fd)
                parent_identity = (parent.st_dev, parent.st_ino)
                try:
                    current = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    pass
                else:
                    current_identity = (current.st_dev, current.st_ino)
            name = os.fsdecode(path)
            if (
                not crashed
                and parent_identity == links_alias.parent_identity
                and current_identity == links_alias.file_identity
                and name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
                and MODULE._pending_cleanup_active_entry_binding(
                    name,
                    links_alias.parent_identity,
                )
                is None
            ):
                crashed = True
                fallback_name = name
                raise SystemExit("injected crash after fallback rename")
            real_unlink(path, *args, dir_fd=dir_fd, **kwargs)

        with (
            mock.patch.object(MODULE.os, "fpathconf", return_value=143),
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=crash_before_fallback_unlink,
            ),
            self.assertRaisesRegex(SystemExit, "after fallback rename"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(crashed)
        self.assertIsNotNone(fallback_name)
        assert fallback_name is not None
        alias_parent = self._directory_with_identity(
            ticket.batch_root,
            links_alias.parent_identity,
        )
        self.assertTrue((alias_parent / fallback_name).is_file())
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_does_not_guess_ambiguous_v1_active_slot(
        self,
    ) -> None:
        parent_identity = (1, 2)
        planned = (3, 4, stat.S_IFREG)
        physical_name = (
            f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}"
            f"1-2-3-4-{stat.S_IFREG:x}-{'0' * 16}"
        )
        legacy_name = MODULE._pending_cleanup_legacy_active_ledger_name(
            parent_identity,
            planned,
        )
        legacy_path = PurePosixPath("links", legacy_name)
        identity_ledger = {
            parent_identity: (
                (
                    physical_name,
                    planned,
                    True,
                    False,
                    physical_name,
                    True,
                    legacy_path,
                    0o600,
                    os.geteuid(),
                    os.getegid(),
                ),
            ),
        }
        aliases = tuple(
            MODULE.PendingTerminalValidationAlias(
                path=PurePosixPath("links", name),
                parent_identity=parent_identity,
                file_identity=planned[:2],
            )
            for name in ("first.toml", "second.toml")
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "legacy active alias cannot be uniquely resolved",
        ):
            MODULE._pending_terminal_validation_current_by_path(
                identity_ledger,
                aliases,
            )

    def test_terminal_validation_rejects_legacy_synthetic_name_collision(
        self,
    ) -> None:
        parent_identity = (1, 2)
        planned = (3, 4, stat.S_IFREG)
        physical_name = (
            f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}"
            f"1-2-3-4-{stat.S_IFREG:x}-{'0' * 16}"
        )
        legacy_name = MODULE._pending_cleanup_legacy_active_ledger_name(
            parent_identity,
            planned,
        )
        legacy_path = PurePosixPath("links", legacy_name)
        identity_ledger = {
            parent_identity: (
                (
                    physical_name,
                    planned,
                    True,
                    False,
                    physical_name,
                    True,
                    legacy_path,
                    0o600,
                    os.geteuid(),
                    os.getegid(),
                ),
            ),
        }
        aliases = tuple(
            MODULE.PendingTerminalValidationAlias(
                path=path,
                parent_identity=parent_identity,
                file_identity=planned[:2],
            )
            for path in (legacy_path, PurePosixPath("links", "second.toml"))
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "legacy active alias cannot be uniquely resolved",
        ):
            MODULE._pending_terminal_validation_current_by_path(
                identity_ledger,
                aliases,
            )

    def test_odd_hex_v2_active_entry_is_not_bound(self) -> None:
        name = (
            f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}"
            f"v2-1-2-3-4-{stat.S_IFREG:x}-abc-{'0' * 16}"
        )
        self.assertIsNone(MODULE._pending_cleanup_active_entry_binding(name, (1, 2)))

    def test_v2_active_entry_rejects_non_component_logical_name(self) -> None:
        for encoded_name in (b"foo/bar", b"foo\0bar"):
            with self.subTest(encoded_name=encoded_name):
                name = (
                    f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}"
                    f"v2-1-2-3-4-{stat.S_IFREG:x}-"
                    f"{encoded_name.hex()}-{'0' * 16}"
                )
                self.assertIsNone(
                    MODULE._pending_cleanup_active_entry_binding(name, (1, 2))
                )

    def test_v2_active_entry_falls_back_when_target_name_max_is_143(self) -> None:
        directory = self.root / "active-token-name-max"
        directory.mkdir(mode=0o700)
        logical_name = "x" * MODULE.MAX_PENDING_CLEANUP_ACTIVE_LOGICAL_NAME_BYTES
        content = directory / logical_name
        content.write_text("backup\n", encoding="utf-8")
        content.chmod(0o600)
        directory_fd = MODULE._open_directory_beneath(self.root, directory)
        try:
            parent_identity = MODULE._directory_identity(directory_fd)
            planned = MODULE._pending_cleanup_entry_plan(
                os.stat(content.name, dir_fd=directory_fd, follow_symlinks=False)
            )
            with mock.patch.object(MODULE.os, "fpathconf", return_value=143):
                active_name, _active = MODULE._isolate_pending_cleanup_entry(
                    directory_fd,
                    content.name,
                    parent_identity,
                    planned,
                    relative_parts=("links",),
                )
        finally:
            MODULE._close_fd_quietly(directory_fd)

        self.assertTrue(
            active_name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
        )
        self.assertFalse(
            active_name.startswith(f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}v2-")
        )
        self.assertLessEqual(len(os.fsencode(active_name)), 143)
        self.assertEqual(
            MODULE._pending_cleanup_internal_entry_plan(
                active_name,
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                parent_identity,
            ),
            planned,
        )
        (directory / active_name).unlink()

    def test_active_cleanup_token_preserves_short_links_content_name(self) -> None:
        links = self.root / "links-token"
        links.mkdir(mode=0o700)
        content = links / "backup.toml"
        content.write_text("backup\n", encoding="utf-8")
        content.chmod(0o600)
        directory_fd = MODULE._open_directory_beneath(self.root, links)
        try:
            parent_identity = MODULE._directory_identity(directory_fd)
            planned = MODULE._pending_cleanup_entry_plan(
                os.stat(content.name, dir_fd=directory_fd, follow_symlinks=False)
            )
            active_name, _active = MODULE._isolate_pending_cleanup_entry(
                directory_fd,
                content.name,
                parent_identity,
                planned,
                relative_parts=("links",),
            )
        finally:
            MODULE._close_fd_quietly(directory_fd)

        binding = MODULE._pending_cleanup_active_entry_binding(
            active_name,
            parent_identity,
        )
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding[0], planned)
        self.assertEqual(binding[1], content.name)

    def test_terminal_validation_partial_cleanup_still_rejects_foreign_hardlink(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self._crash_after_walker_alias_unlink(
            ticket,
            ticket.batch_root / "pending" / "stage",
        )
        foreign = self.root / "foreign-after-receipt.toml"
        os.link(self.target, foreign)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unauthorized hard-link alias",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(foreign.is_file())
        foreign.unlink()
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_rejects_replaced_recovery_alias_after_receipt(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        quarantine_root = ticket.batch_root.parent
        quarantine_fd = MODULE._open_directory_beneath(self.home, quarantine_root)
        batch_fd = MODULE._open_directory_beneath(self.home, ticket.batch_root)
        try:
            MODULE._ensure_pending_terminal_validation_receipt(
                self.home,
                ticket,
                ticket.batch_root,
                batch_fd,
                MODULE._directory_identity(quarantine_fd),
                namespace_anchor_sha256=ticket.terminal_namespace_sha256,
            )
        finally:
            MODULE._close_fd_quietly(batch_fd)
            MODULE._close_fd_quietly(quarantine_fd)

        alias = ticket.batch_root / MODULE._pending_terminal_recovery_alias_name(0)
        alias.unlink()
        alias.write_text("foreign replacement\n", encoding="utf-8")
        alias.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal recovery alias changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(alias.is_file())

    def test_v8_legacy_terminal_receipt_without_alias_map_requires_recovery(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            receipt_path,
            MODULE._pending_cleanup_terminal_validation_payload(
                ticket,
                quarantine_identity,
            ),
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "legacy terminal validation receipt lacks namespace authority; "
            "manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(receipt_path.is_file())

    def test_v4_terminal_receipt_without_alias_map_fails_closed_before_resume(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        legacy_ticket = MODULE.replace(
            ticket,
            version=MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION,
        )
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        batch_fd = MODULE._open_directory_beneath(self.home, ticket.batch_root)
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
            receipt_path = MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            )
            MODULE._publish_atomic_exclusive_internal_file(
                self.home,
                receipt_path,
                MODULE._pending_cleanup_terminal_validation_payload(
                    legacy_ticket,
                    quarantine_identity,
                ),
            )
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "legacy terminal validation ticket lacks namespace authority; "
                "manual recovery is required",
            ):
                MODULE._ensure_pending_terminal_validation_receipt(
                    self.home,
                    legacy_ticket,
                    ticket.batch_root,
                    batch_fd,
                    quarantine_identity,
                )
            self.assertTrue(receipt_path.is_file())
        finally:
            MODULE._close_fd_quietly(batch_fd)
            MODULE._close_fd_quietly(quarantine_fd)

    def test_v8_legacy_v2_terminal_receipt_without_namespace_map_requires_recovery(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        batch_fd = MODULE._open_directory_beneath(self.home, ticket.batch_root)
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
            MODULE._ensure_pending_terminal_validation_receipt(
                self.home,
                ticket,
                ticket.batch_root,
                batch_fd,
                quarantine_identity,
                namespace_anchor_sha256=ticket.terminal_namespace_sha256,
            )
            receipt = MODULE._read_pending_cleanup_terminal_validation(
                self.home,
                ticket,
                quarantine_identity,
            )
            self.assertIsNotNone(receipt)
            assert receipt is not None
            authority = MODULE._parse_pending_terminal_validation_authority(
                self.home,
                ticket,
                quarantine_identity,
                receipt,
            )
            self.assertIsNotNone(authority)
            assert authority is not None
        finally:
            MODULE._close_fd_quietly(batch_fd)
            MODULE._close_fd_quietly(quarantine_fd)

        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        receipt_path.unlink()
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            receipt_path,
            MODULE._pending_cleanup_terminal_validation_v2_payload(
                ticket,
                quarantine_identity,
                authority.aliases,
            ),
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "legacy terminal validation receipt lacks namespace authority; "
            "manual recovery is required",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(receipt_path.is_file())

    def test_terminal_validation_rejects_replaced_recovery_alias_before_receipt(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        alias = ticket.batch_root / MODULE._pending_terminal_recovery_alias_name(0)
        alias.write_text("foreign\n", encoding="utf-8")
        alias.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal recovery alias changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).exists()
        )
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(alias.is_file())
        alias.unlink()
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_resumes_identity_bound_active_subtree(self) -> None:
        ticket = self._deferred_terminal_ticket()
        real_rename = MODULE._rename_noreplace_at
        crashed = False

        def crash_after_pending_directory_isolation(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal crashed
            real_rename(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
            )
            if (
                not crashed
                and source_name == "pending"
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
            ):
                crashed = True
                raise SystemExit("injected active subtree rename crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_pending_directory_isolation,
            ),
            self.assertRaisesRegex(SystemExit, "active subtree rename crash"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(crashed)
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.batch_root.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_validation_rejects_replaced_active_subtree(self) -> None:
        ticket = self._deferred_terminal_ticket()
        real_rename = MODULE._rename_noreplace_at

        def crash_after_pending_directory_isolation(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
            )
            if source_name == "pending" and destination_name.startswith(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
            ):
                raise SystemExit("injected active subtree rename crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_pending_directory_isolation,
            ),
            self.assertRaisesRegex(SystemExit, "active subtree rename crash"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        active = next(
            path
            for path in ticket.batch_root.iterdir()
            if path.name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
            and path.is_dir()
        )
        displaced = self.root / "displaced-active-subtree"
        active.rename(displaced)
        active.mkdir(mode=0o700)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending cleanup active entry changed",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(active.is_dir())
        self.assertTrue(displaced.is_dir())

    def test_terminal_validation_rejects_unknown_active_subtree_entry(self) -> None:
        ticket = self._deferred_terminal_ticket()
        real_rename = MODULE._rename_noreplace_at

        def crash_after_pending_directory_isolation(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
            )
            if source_name == "pending" and destination_name.startswith(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
            ):
                raise SystemExit("injected active subtree rename crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_pending_directory_isolation,
            ),
            self.assertRaisesRegex(SystemExit, "active subtree rename crash"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        active = next(
            path
            for path in ticket.batch_root.iterdir()
            if path.name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
            and path.is_dir()
        )
        foreign = active / "unrecognized-foreign-evidence"
        foreign.write_text("foreign\n", encoding="utf-8")
        foreign.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unknown entry",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(active.is_dir())
        self.assertTrue(foreign.is_file())

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

        ticket_path = next(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
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

    def test_scanner_rejects_same_inode_v8_ticket_downgrade_to_empty_group(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        before = pending_authority_snapshot(self.home)
        ticket_key = ticket.path.relative_to(self.home).as_posix()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["terminal_regular_targets"] = []
        self._rewrite_ticket_same_inode(ticket, payload)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "could not be safely classified",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        after = pending_authority_snapshot(self.home)
        self.assertEqual(
            (ticket.path.stat().st_dev, ticket.path.stat().st_ino),
            ticket.snapshot.file_identity,
        )
        for path, snapshot in before.items():
            if path != ticket_key:
                self.assertEqual(after.get(path), snapshot, path)
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertEqual(self.target.read_bytes(), PUBLIC_PAYLOAD.encode())

    def test_scanner_rejects_same_inode_v4_downgrade_to_empty_group(self) -> None:
        ticket = self._deferred_terminal_ticket()
        before = pending_authority_snapshot(self.home)
        ticket_key = ticket.path.relative_to(self.home).as_posix()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["version"] = MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION
        payload["terminal_regular_targets"] = []
        for field in (
            "commit_evidence",
            "terminal_namespace_sha256",
            "pointer_retirement_path",
            "pointer_retirement",
        ):
            payload.pop(field, None)
        self._rewrite_ticket_same_inode(ticket, payload)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal regular-file validation was retained.*cannot prove",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        after = pending_authority_snapshot(self.home)
        self.assertEqual(
            (ticket.path.stat().st_dev, ticket.path.stat().st_ino),
            ticket.snapshot.file_identity,
        )
        for path, snapshot in before.items():
            if path != ticket_key:
                self.assertEqual(after.get(path), snapshot, path)
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertEqual(self.target.read_bytes(), PUBLIC_PAYLOAD.encode())

    def test_scanner_rejects_v4_empty_downgrade_without_legacy_metadata(self) -> None:
        ticket = self._deferred_terminal_ticket()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["version"] = MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION
        payload["terminal_regular_targets"] = []
        for field in (
            "commit_evidence",
            "terminal_namespace_sha256",
            "pointer_retirement_path",
            "pointer_retirement",
        ):
            payload.pop(field, None)
        for metadata_name in (MODULE.PENDING_LINK_METADATA_NAME, "metadata.json"):
            metadata_path = ticket.batch_root / metadata_name
            if metadata_path.exists():
                metadata_path.unlink()
        self._rewrite_ticket_same_inode(ticket, payload)
        before = pending_authority_snapshot(self.home)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal regular-file validation was retained|metadata is missing|could not be safely classified",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        after = pending_authority_snapshot(self.home)
        self.assertEqual(after, before)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertEqual(self.target.read_bytes(), PUBLIC_PAYLOAD.encode())

    def test_marker_only_v4_ticket_rejects_external_hardlink(self) -> None:
        ticket = self._deferred_terminal_ticket()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["version"] = MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION
        payload["terminal_regular_targets"] = []
        for field in (
            "commit_evidence",
            "terminal_namespace_sha256",
            "pointer_retirement_path",
            "pointer_retirement",
        ):
            payload.pop(field, None)
        self._rewrite_ticket_same_inode(ticket, payload)
        foreign = self.root / "foreign-marker-only-v4-ticket.json"
        os.link(ticket.path, foreign)
        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup control has an unauthorized hard-link alias",
            ):
                MODULE._cleanup_ready_pending_batches(self.home)
            self.assertTrue(ticket.path.is_file())
            self.assertTrue(ticket.batch_root.is_dir())
            self.assertTrue(foreign.is_file())
        finally:
            foreign.unlink()

    def test_marker_only_v4_without_commit_evidence_publishes_control_receipt(
        self,
    ) -> None:
        ticket = self._marker_only_v4_ticket()
        evidence = ticket.batch_root / Path(*MODULE.PENDING_STATE_COMMIT_EVIDENCE.parts)
        if evidence.exists():
            evidence.unlink()

        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )

        def stop_after_receipt(*args: object, **kwargs: object) -> None:
            self.assertTrue(receipt_path.is_file())
            raise SystemExit("injected after marker-only receipt")

        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=stop_after_receipt,
            ),
            self.assertRaisesRegex(SystemExit, "after marker-only receipt"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        receipt = MODULE._read_pending_cleanup_terminal_validation(
            self.home,
            ticket,
            (ticket.batch_root.parent.stat().st_dev, ticket.batch_root.parent.stat().st_ino),
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        authority = MODULE._parse_pending_terminal_validation_authority(
            self.home,
            ticket,
            (ticket.batch_root.parent.stat().st_dev, ticket.batch_root.parent.stat().st_ino),
            receipt,
        )
        self.assertIsNotNone(authority)
        assert authority is not None
        control_paths = {
            path.path
            for control in authority.control_files
            for path in control.paths
        }
        self.assertIn(ticket.marker_path, control_paths)
        self.assertIn(
            PurePosixPath(MODULE.PENDING_LINK_METADATA_NAME),
            control_paths,
        )
        self.assertNotIn(MODULE.PENDING_STATE_COMMIT_EVIDENCE, control_paths)

        marker = ticket.batch_root / Path(*ticket.marker_path.parts)
        marker.write_bytes(b"marker rewritten after receipt\n")
        marker.chmod(0o600)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "receipt-bound entry changed|marker authority changed",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

    def test_marker_only_v4_receipt_rejects_new_batch_namespace_entry(self) -> None:
        ticket = self._marker_only_v4_ticket()
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        self.assertFalse(receipt_path.exists())

        # Publish the marker-only receipt while the batch has its original
        # namespace, then add a links-content entry before the retry. The
        # receipt must not adopt that entry as new deletion authority.
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected after marker-only receipt"),
            ),
            self.assertRaisesRegex(
                SystemExit,
                "injected after marker-only receipt",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)
        self.assertTrue(receipt_path.is_file())

        links_root = ticket.batch_root / "links"
        links_root.mkdir(mode=0o700)
        foreign = links_root / "foreign"
        foreign.write_bytes(b"foreign\n")
        foreign.chmod(0o600)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending terminal validation namespace changed",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertTrue(foreign.is_file())

    def test_marker_only_v4_receipt_anchor_rejects_same_inode_rewrite(self) -> None:
        ticket = self._marker_only_v4_ticket()
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected after marker-only receipt"),
            ),
            self.assertRaisesRegex(
                SystemExit,
                "injected after marker-only receipt",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        links_root = ticket.batch_root / "links"
        links_root.mkdir(mode=0o700)
        foreign = links_root / "foreign"
        foreign.write_bytes(b"foreign\n")
        foreign.chmod(0o600)
        links_metadata = links_root.stat()
        metadata = foreign.stat()
        namespace_entries = payload.get("namespace_entries")
        self.assertIsInstance(namespace_entries, list)
        assert isinstance(namespace_entries, list)
        namespace_entries.append(
            [
                "links",
                MODULE._identity_payload(
                    (ticket.batch_root.stat().st_dev, ticket.batch_root.stat().st_ino)
                ),
                [links_metadata.st_dev, links_metadata.st_ino, stat.S_IFDIR],
                stat.S_IMODE(links_metadata.st_mode),
                links_metadata.st_uid,
                links_metadata.st_gid,
            ]
        )
        namespace_entries.append(
            [
                "links/foreign",
                MODULE._identity_payload(
                    (links_metadata.st_dev, links_metadata.st_ino)
                ),
                [metadata.st_dev, metadata.st_ino, stat.S_IFREG],
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
            ]
        )
        namespace_entries.sort(key=lambda entry: entry[0])
        encoded = MODULE._bounded_json_document(
            payload,
            max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup validation receipt exceeds the size limit",
        )
        with receipt_path.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        receipt_path.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending cleanup empty proof changed|receipt authority changed",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(foreign.is_file())

    def test_marker_only_v4_receipt_and_proof_cannot_expand_namespace_together(
        self,
    ) -> None:
        ticket = self._marker_only_v4_ticket()
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected after marker-only receipt"),
            ),
            self.assertRaisesRegex(
                SystemExit,
                "injected after marker-only receipt",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        links_root = ticket.batch_root / "links"
        links_root.mkdir(mode=0o700)
        foreign = links_root / "foreign"
        foreign.write_bytes(b"foreign\n")
        foreign.chmod(0o600)
        links_metadata = links_root.stat()
        foreign_metadata = foreign.stat()

        receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertIsInstance(receipt_payload, dict)
        assert isinstance(receipt_payload, dict)
        namespace_entries = receipt_payload.get("namespace_entries")
        self.assertIsInstance(namespace_entries, list)
        assert isinstance(namespace_entries, list)
        namespace_entries.extend(
            [
                [
                    "links",
                    MODULE._identity_payload(
                        (ticket.batch_root.stat().st_dev, ticket.batch_root.stat().st_ino)
                    ),
                    [links_metadata.st_dev, links_metadata.st_ino, stat.S_IFDIR],
                    stat.S_IMODE(links_metadata.st_mode),
                    links_metadata.st_uid,
                    links_metadata.st_gid,
                ],
                [
                    "links/foreign",
                    MODULE._identity_payload(
                        (links_metadata.st_dev, links_metadata.st_ino)
                    ),
                    [foreign_metadata.st_dev, foreign_metadata.st_ino, stat.S_IFREG],
                    stat.S_IMODE(foreign_metadata.st_mode),
                    foreign_metadata.st_uid,
                    foreign_metadata.st_gid,
                ],
            ]
        )
        namespace_entries.sort(key=lambda entry: entry[0])
        rewritten_receipt = MODULE._bounded_json_document(
            receipt_payload,
            max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup validation receipt exceeds the size limit",
        )
        with receipt_path.open("wb") as stream:
            stream.write(rewritten_receipt)
            stream.flush()
            os.fsync(stream.fileno())
        receipt_path.chmod(0o600)

        proof_payload = json.loads(proof_path.read_text(encoding="utf-8"))
        self.assertIsInstance(proof_payload, dict)
        assert isinstance(proof_payload, dict)
        receipt_authority = proof_payload.get("terminal_validation_receipt")
        self.assertIsInstance(receipt_authority, dict)
        assert isinstance(receipt_authority, dict)
        receipt_file = receipt_authority.get("file")
        self.assertIsInstance(receipt_file, dict)
        assert isinstance(receipt_file, dict)
        receipt_file["sha256"] = hashlib.sha256(rewritten_receipt).hexdigest()
        receipt_file["size"] = len(rewritten_receipt)
        rewritten_proof = MODULE._bounded_json_document(
            proof_payload,
            max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup empty proof exceeds the size limit",
        )
        with proof_path.open("wb") as stream:
            stream.write(rewritten_proof)
            stream.flush()
            os.fsync(stream.fileno())
        proof_path.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "bounded control set|empty proof changed|namespace authority",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(foreign.is_file())

    def test_marker_only_v4_receipt_rejects_external_hardlink_at_retry(self) -> None:
        ticket = self._marker_only_v4_ticket()
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=SystemExit("injected after marker-only receipt"),
            ),
            self.assertRaisesRegex(
                SystemExit,
                "injected after marker-only receipt",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)
        foreign = self.root / "foreign-marker-only-receipt"
        os.link(receipt_path, foreign)
        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "unauthorized hard-link alias",
            ):
                MODULE._cleanup_ready_pending_batches(self.home)
        finally:
            foreign.unlink()

    def test_marker_only_v4_historic_empty_proof_retries_without_receipt(self) -> None:
        ticket = self._marker_only_v4_ticket()
        quarantine_root = ticket.batch_root.parent
        quarantine_identity = (
            quarantine_root.stat().st_dev,
            quarantine_root.stat().st_ino,
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        MODULE._publish_pending_cleanup_empty_proof(
            self.home,
            ticket,
            quarantine_identity,
        )
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).exists()
        )
        self.assertTrue(proof_path.is_file())
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())
        self.assertFalse(proof_path.exists())

    def test_marker_only_v4_requires_ticket_before_first_walker_mutation(self) -> None:
        ticket = self._marker_only_v4_ticket()
        real_remove = MODULE._remove_pending_batch_directory_contents

        def unlink_ticket_then_walk(*args: object, **kwargs: object) -> object:
            ticket.path.unlink()
            with mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                real_remove,
            ):
                return real_remove(*args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=unlink_ticket_then_walk,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.batch_root.is_dir())

    def test_marker_only_v4_recovers_after_metadata_isolation_crash(self) -> None:
        ticket = self._deferred_terminal_ticket()
        payload = json.loads(ticket.path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        payload["version"] = MODULE.LEGACY_PENDING_TERMINAL_CLEANUP_TICKET_VERSION
        payload["terminal_regular_targets"] = []
        for field in (
            "commit_evidence",
            "terminal_namespace_sha256",
            "pointer_retirement_path",
            "pointer_retirement",
        ):
            payload.pop(field, None)
        self._rewrite_ticket_same_inode(ticket, payload)
        (ticket.batch_root / MODULE.PENDING_LINK_METADATA_NAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "created_at": "2026-09-20T00:00:00Z",
                    "actions": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(ticket.batch_root / "links")
        shutil.rmtree(ticket.batch_root / "state", ignore_errors=True)
        pending_root = ticket.batch_root / "pending"
        for child in pending_root.iterdir():
            if child.name != "state":
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        state_root = pending_root / "state"
        for child in state_root.iterdir():
            if child.name not in {"committed", "commit-evidence"}:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        legacy_ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
        self.assertIsNotNone(legacy_ticket)
        assert legacy_ticket is not None
        batch_fd = MODULE._open_directory_beneath(self.home, legacy_ticket.batch_root)
        try:
            metadata_snapshot = MODULE._read_managed_state_file_snapshot(
                self.home,
                legacy_ticket.batch_root / MODULE.PENDING_LINK_METADATA_NAME,
                batch_fd,
            )
            self.assertTrue(
                MODULE._legacy_marker_only_v4_empty_metadata_is_admitted(
                    self.home,
                    legacy_ticket,
                    batch_fd,
                    metadata_snapshot,
                )
            )
        finally:
            MODULE._close_fd_quietly(batch_fd)

        real_isolate = MODULE._isolate_pending_cleanup_entry
        crashed = False

        def isolate_then_crash(*args: object, **kwargs: object):
            nonlocal crashed
            result = real_isolate(*args, **kwargs)
            name = args[1] if len(args) > 1 else None
            if not crashed and name in {
                MODULE.PENDING_LINK_METADATA_NAME,
                "metadata.json",
            }:
                crashed = True
                raise SystemExit("injected marker-only isolation crash")
            return result

        with (
            mock.patch.object(
                MODULE,
                "_isolate_pending_cleanup_entry",
                side_effect=isolate_then_crash,
            ),
            self.assertRaisesRegex(
                SystemExit,
                "injected marker-only isolation crash",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, legacy_ticket)

        self.assertTrue(crashed)
        batch_metadata = legacy_ticket.batch_root.stat()
        batch_identity = (batch_metadata.st_dev, batch_metadata.st_ino)
        self.assertTrue(
            any(
                MODULE._pending_cleanup_active_entry_binding(
                    child.name,
                    batch_identity,
                )
                for child in legacy_ticket.batch_root.iterdir()
                if child.name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
            )
        )
        self.assertTrue(MODULE._cleanup_ready_pending_batches(self.home))
        self.assertFalse(legacy_ticket.batch_root.exists())

    def test_marker_only_control_scan_has_global_entry_budget(self) -> None:
        scan_root = self.root / "bounded-control-scan"
        scan_root.mkdir(mode=0o700)
        current = scan_root
        for index in range(6):
            current = current / f"branch-{index}"
            current.mkdir(mode=0o700)
        scan_fd = os.open(scan_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with (
                mock.patch.object(MODULE, "MAX_PENDING_CLEANUP_ENTRIES", 4),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending cleanup control scan exceeds the entry budget",
                ),
            ):
                MODULE._find_pending_cleanup_control_snapshot(
                    self.home,
                    scan_root,
                    scan_fd,
                    PurePosixPath("target"),
                    expected_identity=None,
                )
        finally:
            MODULE._close_fd_quietly(scan_fd)

    def test_marker_only_v4_recovers_after_control_unlink_crash(self) -> None:
        ticket = self._marker_only_v4_ticket()
        real_unlink = os.unlink
        crashed = False

        def unlink_then_crash(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            dir_fd: int | None = None,
            **kwargs: object,
        ) -> None:
            nonlocal crashed
            real_unlink(path, *args, dir_fd=dir_fd, **kwargs)
            if (
                not crashed
                and isinstance(path, str)
                and path.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
            ):
                crashed = True
                raise SystemExit("injected marker-only control unlink crash")

        with (
            mock.patch.object(MODULE.os, "unlink", side_effect=unlink_then_crash),
            self.assertRaisesRegex(
                SystemExit,
                "injected marker-only control unlink crash",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(crashed)
        self.assertTrue(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                ticket.batch_root.name,
            ).is_file()
        )
        self.assertTrue(
            MODULE._pending_cleanup_empty_proof_path(
                self.home,
                ticket.batch_root.name,
            ).is_file()
        )
        self.assertTrue(MODULE._cleanup_ready_pending_batches(self.home))
        self.assertFalse(ticket.batch_root.exists())

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
            mutation_revalidator=None,
            **kwargs: object,
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
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
                mutation_revalidator=mutation_revalidator,
                **kwargs,
            )

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

    def test_terminal_retirement_authority_recovers_after_ticket_delete_crash(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file
        retirement_path = MODULE._pending_cleanup_terminal_retirement_path(
            self.home,
            ticket.batch_root.name,
        )
        receipt_retirement_path = (
            MODULE._pending_cleanup_terminal_validation_retirement_path(
                self.home,
                ticket.batch_root.name,
            )
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        crashed = False

        def crash_after_ticket_delete(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
            mutation_revalidator=None,
            **kwargs: object,
        ) -> None:
            nonlocal crashed
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
                mutation_revalidator=mutation_revalidator,
                **kwargs,
            )
            if path == ticket.path and not crashed:
                crashed = True
                raise SystemExit("injected crash after terminal ticket deletion")

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=crash_after_ticket_delete,
            ),
            self.assertRaisesRegex(
                SystemExit,
                "crash after terminal ticket deletion",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(crashed)
        self.assertFalse(ticket.path.exists())
        self.assertTrue(retirement_path.is_file())
        self.assertEqual(retirement_path.stat().st_nlink, 1)
        self.assertTrue(receipt_retirement_path.is_file())
        self.assertTrue(proof_path.is_file())

        retained_retirement_path = retirement_path.with_name(
            next(MODULE._retained_pending_cleanup_names(retirement_path))
        )
        retirement_path.rename(retained_retirement_path)
        self.assertEqual(
            MODULE._restore_pending_cleanup_control_tombstones(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(0),
            ),
            0,
        )
        self.assertFalse(retirement_path.exists())
        self.assertTrue(retained_retirement_path.is_file())
        retained_retirement_path.rename(retirement_path)

        zero_budget = MODULE.PendingCleanupActionBudget(0)
        self.assertEqual(
            MODULE._restore_pending_cleanup_terminal_retirement_aliases(
                self.home,
                budget=zero_budget,
            ),
            0,
        )
        self.assertFalse(ticket.path.exists())
        self.assertTrue(retirement_path.is_file())
        self.assertEqual(
            MODULE._restore_pending_cleanup_terminal_retirement_aliases(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(1),
            ),
            1,
        )
        self.assertTrue(ticket.path.is_file())

        install(self.public, self.home, SHA_A)

        self.assertFalse(retirement_path.exists())
        self.assertFalse(receipt_retirement_path.exists())
        self.assertFalse(proof_path.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_retirement_recovery_rejects_same_inode_payload_rewrite(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        retirement_path = MODULE._pending_cleanup_terminal_retirement_path(
            self.home,
            ticket.batch_root.name,
        )
        MODULE._ensure_pending_cleanup_terminal_retirement(self.home, ticket)
        ticket.path.unlink()
        self.assertEqual(retirement_path.stat().st_nlink, 1)
        real_link = MODULE.os.link
        tampered = False

        def tamper_before_restore_link(src, dst, *args, **kwargs):
            nonlocal tampered
            if dst == ticket.path.name and not tampered:
                retirement_path.write_bytes(b"tampered retirement authority\n")
                retirement_path.chmod(0o600)
                tampered = True
            return real_link(src, dst, *args, **kwargs)

        with (
            mock.patch.object(MODULE.os, "link", side_effect=tamper_before_restore_link),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retirement authority changed during recovery",
            ),
        ):
            MODULE._restore_pending_cleanup_terminal_retirement_aliases(self.home)
        self.assertTrue(tampered)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(retirement_path.is_file())

    def test_terminal_retirement_authority_recovers_after_marker_tombstone_crash(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file
        marker_path = MODULE._pending_cleanup_terminal_validation_retirement_path(
            self.home,
            ticket.batch_root.name,
        )
        retirement_path = MODULE._pending_cleanup_terminal_retirement_path(
            self.home,
            ticket.batch_root.name,
        )
        crashed = False

        def retain_marker_tombstone_then_fail(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
            mutation_revalidator=None,
            **kwargs: object,
        ) -> None:
            nonlocal crashed
            if path == marker_path and not crashed:
                retained = next(MODULE._retained_pending_cleanup_names(path))
                MODULE._rename_noreplace_at(
                    parent_fd,
                    path.name,
                    parent_fd,
                    retained,
                )
                os.fsync(parent_fd)
                crashed = True
                raise SystemExit("injected marker tombstone crash")
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
                mutation_revalidator=mutation_revalidator,
                **kwargs,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=retain_marker_tombstone_then_fail,
            ),
            self.assertRaisesRegex(SystemExit, "marker tombstone crash"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(crashed)
        self.assertFalse(marker_path.exists())
        self.assertTrue(
            any(
                path.name.startswith(
                    MODULE.PENDING_CLEANUP_RETAINED_PREFIX + marker_path.name + "-"
                )
                for path in marker_path.parent.iterdir()
            )
        )
        self.assertTrue(retirement_path.is_file())
        self.assertFalse(ticket.path.exists())

        install(self.public, self.home, SHA_A)

        self.assertFalse(retirement_path.exists())
        self.assertFalse(
            any(
                path.name.startswith(MODULE.PENDING_CLEANUP_RETAINED_PREFIX)
                for path in marker_path.parent.iterdir()
            )
        )
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_terminal_receipt_rejects_byte_identical_replacement_before_delete(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        real_delete = MODULE._delete_pending_cleanup_terminal_validation
        marker_path = MODULE._pending_cleanup_terminal_validation_retirement_path(
            self.home,
            ticket.batch_root.name,
        )
        replaced = False
        original_identity: tuple[int, int] | None = None

        def replace_marker_before_delete(
            home: Path,
            current_ticket: MODULE.PendingBatchCleanupTicket,
            quarantine_root_identity: tuple[int, int],
            *,
            mutation_revalidator=None,
            receipt_path=None,
            expected_identity=None,
            expected_link_count=1,
            **kwargs: object,
        ) -> None:
            nonlocal replaced, original_identity
            if receipt_path == marker_path and not replaced:
                metadata = marker_path.stat()
                original_identity = (metadata.st_dev, metadata.st_ino)
                payload = marker_path.read_bytes()
                marker_path.unlink()
                marker_path.write_bytes(payload)
                marker_path.chmod(0o600)
                replaced = True
            real_delete(
                home,
                current_ticket,
                quarantine_root_identity,
                mutation_revalidator=mutation_revalidator,
                receipt_path=receipt_path,
                expected_identity=expected_identity,
                expected_link_count=expected_link_count,
                **kwargs,
            )

        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_terminal_validation",
                side_effect=replace_marker_before_delete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed sync state changed before read",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(replaced)
        self.assertIsNotNone(original_identity)
        current_metadata = marker_path.stat()
        self.assertNotEqual(
            (current_metadata.st_dev, current_metadata.st_ino),
            original_identity,
        )
        self.assertFalse(ticket.path.exists())
        self.assertTrue(marker_path.is_file())
        self.assertTrue(
            MODULE._pending_cleanup_empty_proof_path(
                self.home,
                ticket.batch_root.name,
            ).is_file()
        )

    def test_v8_control_retirement_rechecks_foreign_hardlink_after_final_verify(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertEqual(
            ticket.version,
            MODULE.PENDING_TERMINAL_CLEANUP_TICKET_VERSION,
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            self.home,
            ticket.batch_root.name,
        )
        foreign = self.root / "foreign-after-final-verify.toml"
        real_verify = MODULE._verify_final_regular_targets
        injected = False

        def add_foreign_hardlink_after_first_final_verify(
            home: Path,
            current_ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            nonlocal injected
            real_verify(home, current_ticket)
            if not injected:
                os.link(self.target, foreign)
                injected = True

        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=add_foreign_hardlink_after_first_final_verify,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed regular file access policy mismatch",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(injected)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(receipt_path.is_file())
        self.assertTrue(proof_path.is_file())
        self.assertTrue(foreign.is_file())
        self.assertEqual(self.target.stat().st_nlink, 2)

        foreign.unlink()
        MODULE._cleanup_ready_pending_batches(self.home)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(receipt_path.exists())
        self.assertFalse(proof_path.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_pending_cleanup_ticket_rejects_external_hardlink(self) -> None:
        ticket = self._deferred_terminal_ticket()
        foreign = self.root / "foreign-pending-cleanup-ticket.json"
        os.link(ticket.path, foreign)
        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup control has an unauthorized hard-link alias",
            ):
                MODULE._remove_cleanup_ready_batch(self.home, ticket)
            self.assertTrue(ticket.path.is_file())
            self.assertTrue(ticket.batch_root.is_dir())
            self.assertTrue(foreign.is_file())
        finally:
            foreign.unlink()

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

    def test_pending_cleanup_unlink_keeps_final_unlink_after_callback(self) -> None:
        self._deferred_terminal_ticket()
        probe = MODULE._pending_cleanup_index_path(self.home) / "callback-order.json"
        probe.write_bytes(b"callback ordering\n")
        probe.chmod(0o600)
        parent_fd = MODULE._open_directory_beneath(self.home, probe.parent)
        events: list[str] = []

        def callback(_name: str) -> None:
            events.append("callback")

        real_unlink = MODULE.os.unlink

        def record_unlink(*args: object, **kwargs: object) -> None:
            events.append("unlink")
            real_unlink(*args, **kwargs)

        try:
            expected = MODULE._read_managed_state_file_snapshot(
                self.home,
                probe,
                parent_fd,
            )
            with mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=record_unlink,
            ):
                MODULE._isolate_and_delete_pending_cleanup_file(
                    self.home,
                    probe,
                    parent_fd,
                    expected,
                    label="pending cleanup callback ordering",
                    mutation_revalidator=callback,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        self.assertEqual(events[-2:], ["callback", "unlink"])
        self.assertFalse(probe.exists())

    def test_pending_cleanup_unlink_rechecks_identity_after_final_callback(
        self,
    ) -> None:
        self._deferred_terminal_ticket()
        probe = MODULE._pending_cleanup_index_path(self.home) / "callback-rebind.json"
        probe.write_bytes(b"callback rebind\n")
        probe.chmod(0o600)
        parent_path = probe.parent
        parent_fd = MODULE._open_directory_beneath(self.home, parent_path)
        backup_path = parent_path / "callback-rebind-original"
        retained_path: Path | None = None
        calls = 0

        def replace_after_final_callback(name: str) -> None:
            nonlocal calls, retained_path
            calls += 1
            if calls == 3:
                retained_path = parent_path / name
                retained_path.rename(backup_path)
                retained_path.write_bytes(b"callback replacement\n")
                retained_path.chmod(0o600)

        try:
            expected = MODULE._read_managed_state_file_snapshot(
                self.home,
                probe,
                parent_fd,
            )
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "changed after final mutation revalidation",
            ):
                MODULE._isolate_and_delete_pending_cleanup_file(
                    self.home,
                    probe,
                    parent_fd,
                    expected,
                    label="pending cleanup callback rebind",
                    mutation_revalidator=replace_after_final_callback,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertEqual(calls, 3)
        self.assertIsNotNone(retained_path)
        assert retained_path is not None
        self.assertTrue(backup_path.is_file())
        self.assertEqual(retained_path.read_bytes(), b"callback replacement\n")

    def test_pending_cleanup_unlink_rechecks_parent_access_policy(self) -> None:
        self._deferred_terminal_ticket()
        probe = MODULE._pending_cleanup_index_path(self.home) / "parent-policy.json"
        probe.write_bytes(b"parent policy ordering\n")
        probe.chmod(0o600)
        parent_path = probe.parent
        original_mode = stat.S_IMODE(parent_path.stat().st_mode)
        parent_fd = MODULE._open_directory_beneath(self.home, parent_path)
        calls = 0

        def weaken_parent_on_final_callback(_name: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                parent_path.chmod(0o755)

        try:
            expected = MODULE._read_managed_state_file_snapshot(
                self.home,
                probe,
                parent_fd,
            )
            with self.assertRaisesRegex(
                MODULE.SyncError,
                r"(?:parent access policy changed before deletion|changed after final mutation revalidation)",
            ):
                MODULE._isolate_and_delete_pending_cleanup_file(
                    self.home,
                    probe,
                    parent_fd,
                    expected,
                    label="pending cleanup parent policy ordering",
                    mutation_revalidator=weaken_parent_on_final_callback,
                )
        finally:
            parent_path.chmod(original_mode)
            MODULE._close_fd_quietly(parent_fd)

        self.assertFalse(probe.exists())
        self.assertEqual(calls, 3)
        retained = tuple(parent_path.glob(".retained-cleanup-parent-policy.json-*"))
        self.assertEqual(len(retained), 1)

    def test_pending_cleanup_walker_rechecks_parent_access_policy_before_unlink(
        self,
    ) -> None:
        walker_root = self.home / "walker-parent-policy"
        walker_root.mkdir(mode=0o700)
        victim = walker_root / "victim.txt"
        victim.write_bytes(b"walker parent policy\n")
        victim.chmod(0o600)
        original_mode = stat.S_IMODE(walker_root.stat().st_mode)
        directory_fd = MODULE._open_directory_beneath(self.home, walker_root)

        def weaken_parent_before_unlink(
            _logical_path: PurePosixPath,
            stage: str,
        ) -> None:
            if stage == "before_unlink":
                walker_root.chmod(0o755)

        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "mode 0755 != 0700",
            ):
                MODULE._remove_pending_batch_directory_contents(
                    directory_fd,
                    MODULE._directory_identity(directory_fd),
                    MODULE._directory_mount_identity(directory_fd),
                    [MODULE.MAX_PENDING_CLEANUP_ENTRIES],
                    depth=0,
                    mutation_revalidator=weaken_parent_before_unlink,
                )
        finally:
            walker_root.chmod(original_mode)
            MODULE._close_fd_quietly(directory_fd)

        self.assertFalse(victim.exists())
        retained = tuple(
            child
            for child in walker_root.iterdir()
            if child.name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
        )
        self.assertEqual(len(retained), 1)

    def test_pending_cleanup_walker_rechecks_identity_after_unlink_callback(
        self,
    ) -> None:
        walker_root = self.home / "walker-rebind"
        walker_root.mkdir(mode=0o700)
        victim = walker_root / "victim.txt"
        victim.write_bytes(b"walker original\n")
        victim.chmod(0o600)
        directory_fd = MODULE._open_directory_beneath(self.home, walker_root)
        backup_path = walker_root / "walker-original"

        def replace_after_unlink_callback(
            _logical_path: PurePosixPath,
            stage: str,
        ) -> None:
            if stage == "before_unlink":
                active = next(
                    child
                    for child in walker_root.iterdir()
                    if child.name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
                )
                active.rename(backup_path)
                active.write_bytes(b"walker replacement\n")
                active.chmod(0o600)

        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "object identity changed",
            ):
                MODULE._remove_pending_batch_directory_contents(
                    directory_fd,
                    MODULE._directory_identity(directory_fd),
                    MODULE._directory_mount_identity(directory_fd),
                    [MODULE.MAX_PENDING_CLEANUP_ENTRIES],
                    depth=0,
                    mutation_revalidator=replace_after_unlink_callback,
                )
        finally:
            MODULE._close_fd_quietly(directory_fd)

        self.assertTrue(backup_path.is_file())
        self.assertTrue(
            any(
                child.is_file()
                and child.read_bytes() == b"walker replacement\n"
                for child in walker_root.iterdir()
            )
        )

    def test_pending_cleanup_walker_rechecks_parent_access_policy_before_rmdir(
        self,
    ) -> None:
        walker_root = self.home / "walker-parent-policy-rmdir"
        walker_root.mkdir(mode=0o700)
        victim = walker_root / "victim-directory"
        victim.mkdir(mode=0o700)
        original_mode = stat.S_IMODE(walker_root.stat().st_mode)
        directory_fd = MODULE._open_directory_beneath(self.home, walker_root)

        def weaken_parent_before_rmdir(
            _logical_path: PurePosixPath,
            stage: str,
        ) -> None:
            if stage == "before_rmdir":
                walker_root.chmod(0o755)

        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "mode 0755 != 0700",
            ):
                MODULE._remove_pending_batch_directory_contents(
                    directory_fd,
                    MODULE._directory_identity(directory_fd),
                    MODULE._directory_mount_identity(directory_fd),
                    [MODULE.MAX_PENDING_CLEANUP_ENTRIES],
                    depth=0,
                    mutation_revalidator=weaken_parent_before_rmdir,
                )
        finally:
            walker_root.chmod(original_mode)
            MODULE._close_fd_quietly(directory_fd)

        self.assertFalse(victim.exists())
        retained = tuple(
            child
            for child in walker_root.iterdir()
            if child.name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
        )
        self.assertEqual(len(retained), 1)

    def test_pending_cleanup_walker_rechecks_identity_after_rmdir_callback(
        self,
    ) -> None:
        walker_root = self.home / "walker-rebind-rmdir"
        walker_root.mkdir(mode=0o700)
        victim = walker_root / "victim-directory"
        victim.mkdir(mode=0o700)
        directory_fd = MODULE._open_directory_beneath(self.home, walker_root)
        backup_path = walker_root / "walker-directory-original"

        def replace_after_rmdir_callback(
            _logical_path: PurePosixPath,
            stage: str,
        ) -> None:
            if stage == "before_rmdir":
                active = next(
                    child
                    for child in walker_root.iterdir()
                    if child.name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
                )
                active.rename(backup_path)
                active.mkdir(mode=0o700)

        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "object identity changed",
            ):
                MODULE._remove_pending_batch_directory_contents(
                    directory_fd,
                    MODULE._directory_identity(directory_fd),
                    MODULE._directory_mount_identity(directory_fd),
                    [MODULE.MAX_PENDING_CLEANUP_ENTRIES],
                    depth=0,
                    mutation_revalidator=replace_after_rmdir_callback,
                )
        finally:
            MODULE._close_fd_quietly(directory_fd)

        self.assertTrue(backup_path.is_dir())
        self.assertTrue(
            any(
                child.is_dir()
                and child.name.startswith(
                    MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX
                )
                for child in walker_root.iterdir()
            )
        )

    def test_v8_orphan_proof_revalidates_group_after_ticket_retirement(self) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertEqual(
            ticket.version,
            MODULE.PENDING_TERMINAL_CLEANUP_TICKET_VERSION,
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        foreign = self.root / "foreign-before-proof-retirement.toml"
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file
        injected = False

        def add_foreign_hardlink_before_proof_retirement(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
            maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
            mutation_revalidator=None,
            **kwargs: object,
        ) -> None:
            nonlocal injected
            if (
                not injected
                and path == proof_path
                and path.name.endswith(MODULE.PENDING_CLEANUP_EMPTY_PROOF_SUFFIX)
            ):
                os.link(self.target, foreign)
                injected = True
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
                maximum_bytes=maximum_bytes,
                mutation_revalidator=mutation_revalidator,
                **kwargs,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=add_foreign_hardlink_before_proof_retirement,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed regular file access policy mismatch",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(injected)
        self.assertFalse(ticket.path.exists())
        self.assertTrue(proof_path.is_file())
        proof = MODULE._read_orphan_pending_cleanup_empty_proof(self.home, proof_path)
        authority = MODULE._parse_pending_cleanup_empty_proof_authority(
            proof_path,
            proof.payload,
        )
        self.assertEqual(authority.version, 3)
        self.assertEqual(authority.source_ticket_version, ticket.version)
        self.assertEqual(self.target.stat().st_nlink, 2)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed regular file access policy mismatch",
        ):
            MODULE._cleanup_orphan_pending_cleanup_empty_proofs(self.home)
        self.assertTrue(proof_path.is_file())

        foreign.unlink()
        self.assertEqual(MODULE._cleanup_orphan_pending_cleanup_empty_proofs(self.home), 1)
        self.assertFalse(proof_path.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_v8_batch_root_rmdir_rejects_foreign_replacement(self) -> None:
        ticket = self._deferred_terminal_ticket()
        isolated = ticket.batch_root.with_name(
            MODULE._pending_cleanup_isolated_batch_name(ticket.batch_root.name)
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        real_require = MODULE._require_pending_cleanup_ticket_unchanged
        replacement_identity: tuple[int, int] | None = None

        def replace_isolated_root_at_rmdir_boundary(home: Path, current) -> None:
            nonlocal replacement_identity
            real_require(home, current)
            if (
                current == ticket
                and proof_path.is_file()
                and isolated.is_dir()
                and replacement_identity is None
            ):
                original_fd = os.open(
                    isolated,
                    MODULE._directory_open_flags(nofollow=True),
                )
                try:
                    isolated.rmdir()
                    isolated.mkdir(mode=0o700)
                    protected = isolated / "foreign"
                    protected.write_bytes(b"foreign batch evidence\n")
                    protected.chmod(0o600)
                    replacement = isolated.stat()
                    replacement_identity = (replacement.st_dev, replacement.st_ino)
                finally:
                    os.close(original_fd)

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_ticket_unchanged",
                side_effect=replace_isolated_root_at_rmdir_boundary,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "batch root changed"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertIsNotNone(replacement_identity)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(proof_path.is_file())
        self.assertTrue(isolated.is_dir())
        self.assertEqual(
            (isolated.stat().st_dev, isolated.stat().st_ino),
            replacement_identity,
        )
        self.assertEqual(
            (isolated / "foreign").read_bytes(),
            b"foreign batch evidence\n",
        )

    def test_v8_terminal_validation_rechecks_batch_root_before_walker_mutation(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        real_remove = MODULE._remove_pending_batch_directory_contents
        replacement = ticket.batch_root.with_name(
            f"{ticket.batch_root.name}-renamed-for-test"
        )
        injected = False

        def replace_root_before_walker_mutation(
            directory_fd: int,
            directory_identity: tuple[int, int],
            root_mount_identity: tuple[int, int | None],
            budget: list[int],
            **kwargs: object,
        ) -> None:
            nonlocal injected
            if not injected and directory_identity == ticket.batch_root_identity:
                ticket.batch_root.rename(replacement)
                ticket.batch_root.mkdir(mode=0o700)
                foreign = ticket.batch_root / "foreign"
                foreign.write_text("foreign batch root\n", encoding="utf-8")
                foreign.chmod(0o600)
                injected = True
            real_remove(
                directory_fd,
                directory_identity,
                root_mount_identity,
                budget,
                **kwargs,
            )

        with (
            mock.patch.object(
                MODULE,
                "_remove_pending_batch_directory_contents",
                side_effect=replace_root_before_walker_mutation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending terminal validation batch root binding changed",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(injected)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())
        self.assertEqual(
            (ticket.batch_root / "foreign").read_text(encoding="utf-8"),
            "foreign batch root\n",
        )
        self.assertTrue(replacement.is_dir())

    def test_v8_proof_retirement_rechecks_batch_root_absence(self) -> None:
        ticket = self._deferred_terminal_ticket()
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        foreign_batch = ticket.batch_root
        real_require = MODULE._require_pending_cleanup_proof_batch_roots_absent
        injected = False

        def replay_batch_at_final_proof_unlink(home: Path, authority) -> None:
            nonlocal injected
            retained_proofs = tuple(
                proof_path.parent.glob(
                    f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{proof_path.name}-*"
                )
            )
            if (
                authority.batch_name == ticket.batch_root.name
                and not injected
                and not proof_path.exists()
                and len(retained_proofs) == 1
            ):
                foreign_batch.mkdir(mode=0o700)
                protected = foreign_batch / "foreign"
                protected.write_bytes(b"foreign batch evidence\n")
                protected.chmod(0o600)
                injected = True
            real_require(home, authority)

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_proof_batch_roots_absent",
                side_effect=replay_batch_at_final_proof_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "empty proof still has a batch root",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(injected)
        self.assertFalse(ticket.path.exists())
        self.assertTrue(foreign_batch.is_dir())
        self.assertEqual(
            (foreign_batch / "foreign").read_bytes(),
            b"foreign batch evidence\n",
        )
        self.assertFalse(proof_path.exists())
        retained_proofs = tuple(
            proof_path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{proof_path.name}-*"
            )
        )
        self.assertEqual(len(retained_proofs), 1)

    def test_v8_orphan_proof_rejects_private_batch_root_residue(self) -> None:
        ticket = self._deferred_terminal_ticket()
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        with mock.patch.object(
            MODULE,
            "_delete_pending_cleanup_empty_proof",
            return_value=None,
        ):
            self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())
        self.assertTrue(proof_path.is_file())
        quarantine_root = ticket.batch_root.parent
        quarantine_fd = MODULE._open_directory_beneath(self.home, quarantine_root)
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
            private_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                quarantine_identity,
                (
                    ticket.batch_root_identity[0],
                    ticket.batch_root_identity[1],
                    stat.S_IFDIR,
                ),
            )
            os.mkdir(private_name, 0o700, dir_fd=quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        private_root = quarantine_root / private_name
        protected = private_root / "foreign"
        protected.write_bytes(b"foreign private batch evidence\n")
        protected.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "empty proof has unresolved private batch-root evidence",
        ):
            MODULE._cleanup_orphan_pending_cleanup_empty_proofs(self.home)

        self.assertTrue(proof_path.is_file())
        self.assertTrue(private_root.is_dir())
        self.assertEqual(protected.read_bytes(), b"foreign private batch evidence\n")

    def test_v8_orphan_proof_rejects_active_links_root_identity_mismatch(self) -> None:
        ticket = self._deferred_terminal_ticket()
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        with mock.patch.object(
            MODULE,
            "_delete_pending_cleanup_empty_proof",
            return_value=None,
        ):
            self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

        quarantine_root = ticket.batch_root.parent
        quarantine_fd = MODULE._open_directory_beneath(self.home, quarantine_root)
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
            private_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_LINKS_ENTRY_PREFIX,
                quarantine_identity,
                (
                    ticket.batch_root_identity[0],
                    ticket.batch_root_identity[1],
                    stat.S_IFDIR,
                ),
            )
            os.mkdir(private_name, 0o700, dir_fd=quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "empty proof has unresolved private batch-root evidence",
        ):
            MODULE._cleanup_orphan_pending_cleanup_empty_proofs(self.home)

        self.assertTrue(proof_path.is_file())
        self.assertTrue((quarantine_root / private_name).is_dir())

    def test_v8_historic_v1_proof_is_parseable_but_cannot_retire_ticket(self) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertEqual(
            ticket.version,
            MODULE.PENDING_TERMINAL_CLEANUP_TICKET_VERSION,
        )
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        historic_authority = MODULE.replace(
            MODULE._pending_cleanup_empty_proof_authority_from_ticket(
                ticket,
                quarantine_identity,
            ),
            version=1,
            terminal_regular_targets=(),
            source_ticket_version=None,
            allocation_control=None,
            allocation_join_metadata=None,
        )
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            proof_path,
            MODULE._pending_cleanup_empty_proof_payload_from_authority(
                historic_authority,
            ),
        )

        self.assertIsNotNone(
            MODULE._read_pending_cleanup_empty_proof(
                self.home,
                ticket,
                quarantine_identity,
            )
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "empty proof lacks v3 final target authority",
        ):
            MODULE._retire_terminal_regular_cleanup_controls(
                self.home,
                ticket,
                quarantine_identity,
            )

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(proof_path.is_file())

    def test_v8_orphan_historic_v1_proof_is_retained_for_manual_recovery(
        self,
    ) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertEqual(
            ticket.version,
            MODULE.PENDING_TERMINAL_CLEANUP_TICKET_VERSION,
        )
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        historic_authority = MODULE.replace(
            MODULE._pending_cleanup_empty_proof_authority_from_ticket(
                ticket,
                quarantine_identity,
            ),
            version=1,
            terminal_regular_targets=(),
            source_ticket_version=None,
            allocation_control=None,
            allocation_join_metadata=None,
        )
        historic_payload = MODULE._pending_cleanup_empty_proof_payload_from_authority(
            historic_authority,
        )
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            proof_path,
            historic_payload,
        )

        # Model the old crash window: the legacy proof permits the batch to
        # empty, but cannot authorize v8 ticket retirement. A historic build
        # could then leave the proof after the ticket vanished.
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "empty proof lacks v3 final target authority",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)
        self.assertFalse(ticket.batch_root.exists())
        self.assertTrue(ticket.path.is_file())
        ticket.path.unlink()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "historic pending cleanup empty proof lacks v3 source authority; "
            "manual recovery is required",
        ):
            MODULE._cleanup_orphan_pending_cleanup_empty_proofs(self.home)

        self.assertFalse(ticket.path.exists())
        self.assertTrue(proof_path.is_file())
        self.assertEqual(proof_path.read_bytes(), historic_payload)
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_v8_historic_v2_proof_requires_ticket_for_recovery(self) -> None:
        ticket = self._deferred_terminal_ticket()
        quarantine_fd = MODULE._open_directory_beneath(
            self.home,
            ticket.batch_root.parent,
        )
        try:
            quarantine_identity = MODULE._directory_identity(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        historic_authority = MODULE.replace(
            MODULE._pending_cleanup_empty_proof_authority_from_ticket(
                ticket,
                quarantine_identity,
            ),
            version=2,
            source_ticket_version=None,
            allocation_control=None,
            allocation_join_metadata=None,
        )
        historic_payload = MODULE._pending_cleanup_empty_proof_payload_from_authority(
            historic_authority,
        )
        MODULE._publish_atomic_exclusive_internal_file(
            self.home,
            proof_path,
            historic_payload,
        )

        # A live exact v8 ticket still supplies the missing source shape, so
        # the historical v2 group can carry this legacy recovery through the
        # ticket-retirement boundary.  Keep the proof to model a crash after
        # that boundary, where its bytes alone must never become authority.
        with mock.patch.object(
            MODULE,
            "_delete_pending_cleanup_empty_proof",
            return_value=None,
        ):
            self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())
        self.assertTrue(proof_path.is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "historic pending cleanup empty proof lacks v3 source authority; "
            "manual recovery is required",
        ):
            MODULE._cleanup_orphan_pending_cleanup_empty_proofs(self.home)

        self.assertTrue(proof_path.is_file())
        self.assertEqual(proof_path.read_bytes(), historic_payload)
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_v8_ticket_retirement_rechecks_proof_before_tombstone(self) -> None:
        ticket = self._deferred_terminal_ticket()
        self.assertEqual(
            ticket.version,
            MODULE.PENDING_TERMINAL_CLEANUP_TICKET_VERSION,
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        real_delete_ticket = MODULE._delete_pending_cleanup_ticket
        replaced = False

        def replace_proof_before_ticket_tombstone(
            home: Path,
            current_ticket: MODULE.PendingBatchCleanupTicket,
            *,
            boundary_revalidator=None,
        ) -> None:
            nonlocal replaced
            if current_ticket.path == ticket.path and not replaced:
                self.assertTrue(proof_path.is_file())
                proof_path.unlink()
                proof_path.write_bytes(b"foreign proof replacement\n")
                proof_path.chmod(0o600)
                replaced = True
            real_delete_ticket(
                home,
                current_ticket,
                boundary_revalidator=boundary_revalidator,
            )

        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_ticket",
                side_effect=replace_proof_before_ticket_tombstone,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup empty proof changed",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(replaced)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(proof_path.is_file())

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
            **kwargs: object,
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
                **kwargs,
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
            status_code = MODULE.main(["status", "--home", str(self.home), "--strict"])
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
            marker_path = batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            ticket = MODULE._mark_pending_batch_staging_cleanup_ready(
                home,
                batch_root,
                batch_identity,
            )
            authority = ticket.path
            if kind == "retained":
                retained_name = next(MODULE._retained_pending_cleanup_names(authority))
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
                    "parent_identity": MODULE._identity_payload(marker.parent_identity),
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
