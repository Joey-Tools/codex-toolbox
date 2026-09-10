from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_regular_agent_materialization import (
    MODULE,
    ROLE_TARGET,
    SHA_A,
    SHA_B,
    install,
    write_release,
)


class PendingStagingCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.first_release = self.root / "first-release"
        self.next_release = self.root / "next-release"
        write_release(self.first_release, role_payload='name = "first"\n')
        write_release(self.next_release, role_payload='name = "next"\n')
        install(self.first_release, self.home, SHA_A)
        self.target = self.home / ROLE_TARGET

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_internal_publication_cleanup_failure(
        self,
        publish,
        *,
        publication_failure: str,
    ) -> None:
        if callable(getattr(MODULE.SyncError("probe"), "add_note", None)):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                publication_failure,
            ) as raised:
                publish()
            creation_error = raised.exception.__cause__
            self.assertIsInstance(
                creation_error,
                MODULE._PendingCleanupAccessPolicyError,
            )
            assert creation_error is not None
            self.assertIn("access policy mismatch", str(creation_error))
            notes = "\n".join(getattr(creation_error, "__notes__", ()))
            self.assertIn("final name could not be safely cleared", notes)
            self.assertIn(
                "failed to allocate a retained pending cleanup name",
                notes,
            )
        else:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "final name could not be safely cleared",
            ) as raised:
                publish()
            self.assertIn("access policy mismatch", str(raised.exception))
            self.assertIn(
                "failed to allocate a retained pending cleanup name",
                str(raised.exception),
            )
            cleanup_error = raised.exception.__cause__
            self.assertIsInstance(cleanup_error, MODULE.SyncError)
            assert cleanup_error is not None
            self.assertIn(
                "failed to allocate a retained pending cleanup name",
                str(cleanup_error),
            )

    def test_existing_quarantine_directory_is_opened_once(self) -> None:
        batch_root = self.root / "batch-existing-directory"
        child = batch_root / "pending"
        child.mkdir(parents=True)
        root_fd = os.open(
            batch_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        real_open = MODULE.os.open
        opened_child_fds: list[int] = []

        def track_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if name == child.name:
                opened_child_fds.append(fd)
            return fd

        returned_fd = -1
        try:
            with mock.patch.object(MODULE.os, "open", side_effect=track_open):
                returned_fd = MODULE._create_quarantine_batch_directory_at(
                    root_fd,
                    (child.name,),
                )
            self.assertEqual(opened_child_fds, [returned_fd])
        finally:
            if returned_fd >= 0:
                os.close(returned_fd)
            os.close(root_fd)

    def test_quarantine_directory_open_closes_child_on_fsync_error(self) -> None:
        batch_root = self.root / "batch-directory-fsync-error"
        batch_root.mkdir()
        root_fd = os.open(
            batch_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        real_open = MODULE.os.open
        opened_child_fds: list[int] = []

        def track_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if name == "pending":
                opened_child_fds.append(fd)
            return fd

        try:
            with (
                mock.patch.object(MODULE.os, "open", side_effect=track_open),
                mock.patch.object(
                    MODULE.os,
                    "fsync",
                    side_effect=OSError("injected directory fsync failure"),
                ),
                self.assertRaisesRegex(OSError, "injected directory fsync failure"),
            ):
                MODULE._create_quarantine_batch_directory_at(
                    root_fd,
                    ("pending",),
                )
            self.assertEqual(len(opened_child_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_child_fds[0])
        finally:
            os.close(root_fd)

    def _fail_after_live_preimage_hardlink(self):
        real_publish = MODULE._publish_regular_hardlink_beneath
        tripped = False

        def publish_then_fail(
            home: Path,
            source: Path,
            destination: Path,
            expected_source,
        ):
            nonlocal tripped
            published = real_publish(
                home,
                source,
                destination,
                expected_source,
            )
            if (
                not tripped
                and source == self.target
                and destination.parent.name == "before"
                and destination.parent.parent.name == "pending"
            ):
                tripped = True
                raise MODULE.SyncError(
                    "injected failure after durable live preimage hardlink"
                )
            return published

        return mock.patch.object(
            MODULE,
            "_publish_regular_hardlink_beneath",
            side_effect=publish_then_fail,
        )

    def _only_cleanup_ticket(self):
        tickets = list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        self.assertEqual(len(tickets), 1)
        ticket = MODULE._read_pending_cleanup_ticket(self.home, tickets[0])
        self.assertIsNotNone(ticket)
        assert ticket is not None
        return ticket

    def _only_cleanup_ticket_for_home(self, home: Path):
        tickets = list(MODULE._pending_cleanup_index_path(home).glob("*.json"))
        self.assertEqual(len(tickets), 1)
        ticket = MODULE._read_pending_cleanup_ticket(home, tickets[0])
        self.assertIsNotNone(ticket)
        assert ticket is not None
        return ticket

    def _receiptless_cleanup_crash_patch(self, stage: str):
        real_unlink = MODULE.os.unlink
        real_rename = MODULE._rename_noreplace_at

        if stage in {"ticket-temp", "canonical-alias", "alias-private"}:

            def crash_during_rename(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
            ) -> None:
                if (
                    stage == "ticket-temp"
                    and source_name.endswith(MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX)
                    and destination_name.endswith(MODULE.PENDING_CLEANUP_TICKET_SUFFIX)
                ):
                    raise SystemExit("injected crash before ticket temp rename")
                real_rename(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )
                if stage == "canonical-alias" and destination_name.startswith(
                    ".codex-publication-cleanup-"
                ):
                    raise SystemExit("injected crash after canonical isolation")
                if stage == "alias-private" and destination_name.startswith(
                    ".codex-ephemeral-cleanup-"
                ):
                    raise SystemExit("injected crash after private isolation")

            return mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_during_rename,
            )
        if stage in {"private-unlink-before", "private-unlink-after"}:

            def crash_during_private_unlink(name: str, *args, **kwargs) -> None:
                if name.startswith(".codex-ephemeral-cleanup-"):
                    if stage == "private-unlink-before":
                        raise SystemExit("injected crash before private unlink")
                    real_unlink(name, *args, **kwargs)
                    raise SystemExit("injected crash after private unlink")
                real_unlink(name, *args, **kwargs)

            return mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=crash_during_private_unlink,
            )
        raise AssertionError(f"unknown crash stage: {stage}")

    @contextlib.contextmanager
    def _recycled_private_inode_identity(self, foreign_private: Path, expected):
        """Model inode reuse while preserving the foreign payload snapshot."""
        real_inventory = MODULE._pending_ephemeral_quarantine_private_inventory
        real_snapshot = MODULE._regular_file_snapshot_at
        snapshot_names: list[str] = []

        def report_recycled_inventory_identity(
            quarantine_fd: int,
            batch_name: str,
        ) -> tuple[tuple[str, tuple[int, int]], ...]:
            return tuple(
                (
                    name,
                    expected.file_identity
                    if name == foreign_private.name
                    else identity,
                )
                for name, identity in real_inventory(quarantine_fd, batch_name)
            )

        def report_recycled_snapshot_identity(
            parent_fd: int,
            name: str,
            path: Path,
            *,
            maximum_bytes: int = MODULE.MAX_ARCHIVE_MEMBER_BYTES,
        ):
            snapshot_names.append(name)
            snapshot = real_snapshot(
                parent_fd,
                name,
                path,
                maximum_bytes=maximum_bytes,
            )
            if path == foreign_private:
                return replace(snapshot, file_identity=expected.file_identity)
            return snapshot

        with (
            mock.patch.object(
                MODULE,
                "_pending_ephemeral_quarantine_private_inventory",
                side_effect=report_recycled_inventory_identity,
            ),
            mock.patch.object(
                MODULE,
                "_regular_file_snapshot_at",
                side_effect=report_recycled_snapshot_identity,
            ),
        ):
            yield snapshot_names

    def _make_v5_empty_ticket(self, home: Path, target: Path):
        expected = MODULE._read_regular_file_snapshot_beneath(
            home,
            target,
            require_managed_access=False,
        )
        parent_fd = MODULE._open_directory_beneath(home, target.parent)
        try:
            result = MODULE._move_regular_leaf_to_unique_quarantine(
                home,
                target.parent,
                parent_fd,
                target.name,
                label="legacy-v5-test",
                expected=expected,
                retain_batch_binding=True,
            )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        quarantine_path, _moved, binding = result
        # This helper models a v5 ticket created before v8 allocation fences
        # existed.  A current allocation keeps its (now retired) v8 binding so
        # production code rejects any fallback reclaim after a private move.
        binding = replace(binding, allocation_ticket=None)
        leaf_fd = MODULE._open_directory_beneath(home, quarantine_path.parent)
        try:
            os.unlink(quarantine_path.name, dir_fd=leaf_fd)
            os.fsync(leaf_fd)
        finally:
            MODULE._close_fd_quietly(leaf_fd)
        metadata = binding.metadata
        self.assertIsNotNone(metadata.file_identity)
        self.assertIsNotNone(metadata.payload)
        assert metadata.file_identity is not None
        assert metadata.payload is not None
        legacy_payload = MODULE._bounded_json_document(
            {
                "version": 5,
                "kind": "ephemeral-quarantine",
                "batch": binding.batch_root.name,
                "batch_root_identity": MODULE._identity_payload(binding.batch_identity),
                "quarantine_root_identity": MODULE._identity_payload(
                    binding.quarantine_root_identity
                ),
                "isolated_name": MODULE._pending_cleanup_isolated_batch_name(
                    binding.batch_root.name
                ),
                "leaf": {
                    "path": "leaf",
                    "directory_identity": MODULE._identity_payload(
                        binding.leaf_identity
                    ),
                },
                "metadata": {
                    "path": "metadata.json",
                    "file_identity": MODULE._identity_payload(metadata.file_identity),
                    "mode": metadata.mode,
                    "sha256": hashlib.sha256(metadata.payload).hexdigest(),
                },
            },
            max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup ticket exceeds the size limit",
        )
        self.assertNotIn(b'"size"', legacy_payload)
        self.assertEqual(
            MODULE._pending_ephemeral_quarantine_cleanup_ticket_payload(
                binding,
                binding.quarantine_root_identity,
            ),
            legacy_payload,
        )
        index_fd = MODULE._open_or_create_directory_beneath(
            home,
            MODULE._pending_cleanup_index_path(home),
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_path = MODULE._pending_cleanup_ticket_path(
            home,
            binding.batch_root.name,
        )
        MODULE._publish_pending_cleanup_ticket(home, ticket_path, legacy_payload)
        ticket = MODULE._read_pending_cleanup_ticket(home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        self.assertIsNone(ticket.metadata_size)
        return ticket

    def _alternate_gid(self, current_gid: int) -> int:
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != current_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        assert alternate_gid is not None
        return alternate_gid

    def _legacy_cleanup_ticket_payload(
        self,
        batch_root: Path,
        *,
        version: int,
    ) -> bytes:
        self.assertIn(version, {1, 2, 3, 4})
        marker_path = {
            1: MODULE.PENDING_STATE_COMMIT_MARKER,
            2: MODULE.PENDING_STATE_ROLLBACK_MARKER,
            3: MODULE.PENDING_STATE_STAGING_MARKER,
            4: MODULE.PENDING_STATE_COMMIT_MARKER,
        }[version]
        marker_parent = batch_root / Path(*marker_path.parent.parts)
        marker_parent.mkdir(parents=True, exist_ok=True)
        control_parent = batch_root
        for part in marker_path.parent.parts:
            control_parent /= part
            control_parent.chmod(0o700)
        marker = MODULE._write_exclusive_internal_file(
            self.home,
            marker_parent / marker_path.name,
            b"legacy cleanup marker\n",
        )
        self.assertIsNotNone(marker.parent_identity)
        self.assertIsNotNone(marker.file_identity)
        assert marker.parent_identity is not None
        assert marker.file_identity is not None
        batch_identity = (batch_root.stat().st_dev, batch_root.stat().st_ino)
        digest = hashlib.sha256(marker.payload or b"").hexdigest()
        if version == 1:
            return MODULE._pending_cleanup_ticket_payload(
                batch_root,
                batch_identity,
                marker.parent_identity,
                marker.file_identity,
                0o600,
                digest,
            )
        payload: dict[str, object] = {
            "version": version,
            "batch": batch_root.name,
            "batch_root_identity": MODULE._identity_payload(batch_identity),
            "finalization_marker": {
                "phase": {2: "before", 3: "staging", 4: "after"}[version],
                "path": marker_path.as_posix(),
                "parent_identity": MODULE._identity_payload(marker.parent_identity),
                "file_identity": MODULE._identity_payload(marker.file_identity),
                "mode": 0o600,
                "sha256": digest,
            },
        }
        if version == 4:
            payload["terminal_regular_targets"] = []
        return MODULE._bounded_json_document(
            payload,
            max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup ticket exceeds the size limit",
        )

    def _publish_legacy_cleanup_ticket(
        self,
        *,
        version: int,
    ) -> MODULE.PendingBatchCleanupTicket:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        payload = self._legacy_cleanup_ticket_payload(batch_root, version=version)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            MODULE._pending_cleanup_index_path(self.home),
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        MODULE._publish_pending_cleanup_ticket(self.home, ticket_path, payload)
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        self.assertEqual(ticket.version, version)
        return ticket

    def _stage_legacy_symlink_pointer(self, metadata_version: int):
        legacy_home = self.root / f"legacy-home-v{metadata_version}"
        first_release = self.root / f"legacy-first-v{metadata_version}"
        next_release = self.root / f"legacy-next-v{metadata_version}"
        write_release(first_release)
        write_release(next_release)
        install(first_release, legacy_home, SHA_A)
        with (
            mock.patch.object(
                MODULE,
                "_retire_pending_staging_cleanup_authority",
                side_effect=MODULE.SyncError("injected active pointer retention"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(next_release, legacy_home, SHA_B)

        batch = MODULE._load_pending_link_batch(legacy_home)
        self.assertIsNotNone(batch)
        assert batch is not None
        MODULE._clear_pending_link_pointer(legacy_home, batch, phase="before")
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        data["version"] = metadata_version
        for field in ("state_before", "state_after", "commit_evidence"):
            data[field].pop("uid")
            data[field].pop("gid")
        data.pop("terminal_regular_before", None)
        data.pop("terminal_regular_after", None)
        for record in data["records"]:
            record.pop("before_materialization")
            record.pop("removed_link")
            record.pop("publication_cleanup")
        if metadata_version < 6:
            for record in data["records"]:
                for field in (
                    "materialization",
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    record.pop(field)
                for field in (
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    record["planned_before"].pop(field)
        metadata_path.write_bytes(
            MODULE._bounded_json_document(
                data,
                max_bytes=MODULE.MAX_MANAGED_STATE_BYTES,
                overflow_error="pending link transaction metadata exceeds the size limit",
            )
        )
        os.link(
            metadata_path,
            MODULE._pending_link_pointer_path(legacy_home),
            follow_symlinks=False,
        )
        restored_batch = MODULE._load_pending_link_batch(legacy_home)
        self.assertIsNotNone(restored_batch)
        assert restored_batch is not None
        ticket_path = MODULE._pending_cleanup_ticket_path(
            legacy_home,
            restored_batch.batch_root.name,
        )
        ticket_path.unlink()
        ticket_path.parent.rmdir()
        return legacy_home, restored_batch

    def test_staging_failure_immediately_removes_live_preimage_hardlink(self) -> None:
        with (
            self._fail_after_live_preimage_hardlink(),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected failure after durable live preimage hardlink",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "first"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_marker_only_staging_batch_is_discovered_on_next_run(self) -> None:
        real_publish = MODULE._publish_pending_batch_cleanup_ticket_for_root
        tripped = False

        def fail_after_marker(
            home: Path,
            batch_root: Path,
            payload: bytes,
        ) -> None:
            nonlocal tripped
            marker = batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
            if not tripped and marker.is_file():
                tripped = True
                raise MODULE.SyncError("injected failure before staging ticket")
            real_publish(home, batch_root, payload)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_batch_cleanup_ticket_for_root",
                side_effect=fail_after_marker,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected failure before staging ticket",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertFalse(ticket_root.exists() and list(ticket_root.glob("*.json")))
        quarantine = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        marker_batches = [
            batch
            for batch in quarantine.iterdir()
            if (batch / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)).is_file()
        ]
        self.assertEqual(len(marker_batches), 1)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(marker_batches[0].exists())

    def test_no_ticket_batch_with_symlinked_marker_parent_fails_closed(self) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        outside = self.root / "outside-marker"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep\n", encoding="utf-8")
        (batch_root / "pending").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending staging marker changed",
        ):
            MODULE._discover_pending_staging_markers(self.home)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_foreign_hardlink_race_is_not_adopted_as_transaction_state(self) -> None:
        real_publish = MODULE._publish_regular_hardlink_beneath
        foreign_alias = self.root / "foreign-live-alias"
        tripped = False

        def publish_then_add_foreign_alias(
            home: Path,
            source: Path,
            destination: Path,
            expected_source,
        ):
            nonlocal tripped
            published = real_publish(
                home,
                source,
                destination,
                expected_source,
            )
            if (
                not tripped
                and source == self.target
                and destination.parent.name == "before"
                and destination.parent.parent.name == "pending"
            ):
                self.assertEqual(source.stat().st_nlink, 2)
                os.link(source, foreign_alias, follow_symlinks=False)
                self.assertEqual(source.stat().st_nlink, 3)
                tripped = True
            return published

        with (
            mock.patch.object(
                MODULE,
                "_publish_regular_hardlink_beneath",
                side_effect=publish_then_add_foreign_alias,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending live regular preimage changed during staging",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertTrue(foreign_alias.is_file())
        self.assertEqual(self.target.stat().st_nlink, 2)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_next_run_cleans_retained_staging_ticket_before_nlink_validation(
        self,
    ) -> None:
        with (
            self._fail_after_live_preimage_hardlink(),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=MODULE.SyncError("injected interrupted cleanup"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "exact cleanup was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 3)
        self.assertEqual(ticket.phase, "staging")
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertEqual(self.target.stat().st_nlink, 2)

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_post_link_staging_failure_persists_manual_retention_guard(
        self,
    ) -> None:
        real_publish = MODULE._publish_regular_hardlink_beneath
        tripped = False

        def publish_then_fail(
            home: Path,
            source: Path,
            destination: Path,
            expected_source,
            **kwargs,
        ):
            nonlocal tripped
            published = real_publish(
                home,
                source,
                destination,
                expected_source,
                **kwargs,
            )
            if (
                not tripped
                and source.parent.name == "stage"
                and destination.parent.name == "evidence"
            ):
                tripped = True
                raise MODULE.SyncError(
                    "injected post-link staging failure",
                    code=MODULE.PENDING_REGULAR_PUBLICATION_RETAINED_CODE,
                )
            return published

        with (
            mock.patch.object(
                MODULE,
                "_publish_regular_hardlink_beneath",
                side_effect=publish_then_fail,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained for manual recovery",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 3)
        cleanup_entries = list((ticket.batch_root / "pending/cleanup").iterdir())
        self.assertEqual(len(cleanup_entries), 1)
        record_leaf = cleanup_entries[0].stem
        stage = ticket.batch_root / "pending/stage" / record_leaf
        evidence = ticket.batch_root / "pending/evidence" / record_leaf
        self.assertEqual(
            (stage.stat().st_dev, stage.stat().st_ino),
            (evidence.stat().st_dev, evidence.stat().st_ino),
        )

        with self.assertRaisesRegex(MODULE.SyncError, "manual cleanup"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(stage.is_file())
        self.assertTrue(evidence.is_file())
        self.assertTrue(cleanup_entries[0].is_file())

    def test_moved_batch_without_empty_proof_retains_cleanup_authority(self) -> None:
        with (
            self._fail_after_live_preimage_hardlink(),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=MODULE.SyncError("injected interrupted cleanup"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "exact cleanup was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        moved_batch = ticket.batch_root.with_name(ticket.batch_root.name + "-moved")
        ticket.batch_root.rename(moved_batch)

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("missing without an exact empty proof", stdout.getvalue())
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(moved_batch.is_dir())
        self.assertEqual(self.target.stat().st_nlink, 2)

        moved_batch.rename(ticket.batch_root)
        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(ticket.path.exists())

    def test_pointer_recovery_retires_staging_ticket_before_rollback(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_retire_pending_staging_cleanup_authority",
                side_effect=MODULE.SyncError("injected staging authority retention"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 3)
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self.assertEqual(self.target.stat().st_nlink, 2)

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_pointer_recovers_after_ticket_deleted_before_marker(self) -> None:
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def fail_marker_delete(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
        ) -> None:
            if label == "pending staging cleanup marker":
                raise MODULE.SyncError("injected staging marker retention")
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=fail_marker_delete,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_active_pointer_recovers_staging_ticket_tombstone(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_retire_pending_staging_cleanup_authority",
                side_effect=MODULE.SyncError("injected staging authority retention"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 3)
        marker = ticket.batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def crash_after_ticket_isolation(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
        ) -> None:
            if path == ticket.path:
                retained = next(MODULE._retained_pending_cleanup_names(path))
                MODULE._rename_noreplace_at(
                    parent_fd,
                    path.name,
                    parent_fd,
                    retained,
                )
                os.fsync(parent_fd)
                raise SystemExit("injected staging ticket tombstone crash")
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=crash_after_ticket_isolation,
            ),
            self.assertRaisesRegex(SystemExit, "tombstone crash"),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertFalse(ticket.path.exists())
        self.assertTrue(marker.is_file())
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        retained = list(
            ticket.path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{ticket.path.name}-*"
            )
        )
        self.assertEqual(len(retained), 1)

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertFalse(retained[0].exists())

    def test_publisher_recovers_retained_ticket_temp(self) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        metadata = batch_root.stat()
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        temp_path = ticket_path.with_name(
            batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(b"{\n")
        temp_path.chmod(0o600)
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def crash_after_temp_isolation(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
        ) -> None:
            if path == temp_path:
                retained = next(MODULE._retained_pending_cleanup_names(path))
                MODULE._rename_noreplace_at(
                    parent_fd,
                    path.name,
                    parent_fd,
                    retained,
                )
                os.fsync(parent_fd)
                raise SystemExit("injected temp tombstone crash")
            real_delete(home, path, parent_fd, expected, label=label)

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=crash_after_temp_isolation,
            ),
            self.assertRaisesRegex(SystemExit, "temp tombstone crash"),
        ):
            MODULE._discard_incomplete_pending_cleanup_ticket(self.home, temp_path)

        retained = list(
            temp_path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{temp_path.name}-*"
            )
        )
        self.assertEqual(len(retained), 1)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        payload = MODULE._pending_cleanup_ticket_payload(
            batch_root,
            (metadata.st_dev, metadata.st_ino),
            (1, 1),
            (1, 2),
            0o600,
            "a" * 64,
        )

        MODULE._publish_pending_cleanup_ticket(self.home, ticket_path, payload)

        self.assertEqual(ticket_path.read_bytes(), payload)
        self.assertFalse(retained[0].exists())
        self.assertFalse(os.path.lexists(temp_path))

    def test_legacy_active_pointer_without_cleanup_directory_recovers_only_before_v6(
        self,
    ) -> None:
        for metadata_version in (4, 5, 6):
            with self.subTest(metadata_version=metadata_version):
                legacy_home, batch = self._stage_legacy_symlink_pointer(
                    metadata_version
                )
                state, state_snapshot = MODULE._load_managed_state_with_snapshot(
                    legacy_home
                )
                if metadata_version < 6:
                    recovered, _snapshot, did_recover = (
                        MODULE._recover_pending_link_transaction(
                            legacy_home,
                            state,
                            state_snapshot,
                            dry_run=False,
                        )
                    )
                    self.assertTrue(did_recover)
                    self.assertEqual(recovered, state)
                    self.assertFalse(
                        os.path.lexists(MODULE._pending_link_pointer_path(legacy_home))
                    )
                else:
                    with self.assertRaises((MODULE.SyncError, OSError)):
                        MODULE._recover_pending_link_transaction(
                            legacy_home,
                            state,
                            state_snapshot,
                            dry_run=False,
                        )
                    self.assertTrue(
                        MODULE._pending_link_pointer_path(legacy_home).is_file()
                    )
                self.assertTrue(batch.batch_root.is_dir())

    def test_rmdir_to_ticket_delete_crash_without_proof_only_recovers_legacy_tickets(
        self,
    ) -> None:
        for version in (1, 2, 3, 4):
            with self.subTest(ticket_version=version):
                case_home = self.root / f"empty-proof-home-v{version}"
                case_home.mkdir()
                original_home = self.home
                self.home = case_home
                try:
                    ticket = self._publish_legacy_cleanup_ticket(version=version)
                    proof_path = MODULE._pending_cleanup_empty_proof_path(
                        self.home,
                        ticket.batch_root.name,
                    )

                    def crash_after_rmdir(
                        _home: Path,
                        _ticket: MODULE.PendingBatchCleanupTicket,
                    ) -> None:
                        self.assertTrue(proof_path.is_file())
                        proof_path.unlink()
                        raise SystemExit("injected rmdir-to-ticket-delete crash")

                    with (
                        mock.patch.object(
                            MODULE,
                            "_delete_pending_cleanup_ticket",
                            side_effect=crash_after_rmdir,
                        ),
                        self.assertRaisesRegex(SystemExit, "ticket-delete crash"),
                    ):
                        MODULE._remove_cleanup_ready_batch(self.home, ticket)

                    self.assertFalse(ticket.batch_root.exists())
                    self.assertFalse(proof_path.exists())
                    self.assertTrue(ticket.path.is_file())
                    if version < 3:
                        self.assertEqual(
                            MODULE._cleanup_ready_pending_batches(self.home),
                            1,
                        )
                        self.assertFalse(ticket.path.exists())
                    elif version == 3:
                        with contextlib.redirect_stdout(io.StringIO()) as stdout:
                            self.assertEqual(
                                MODULE._cleanup_ready_pending_batches(self.home),
                                0,
                            )
                        self.assertIn(
                            "missing without an exact empty proof", stdout.getvalue()
                        )
                        self.assertTrue(ticket.path.is_file())
                    else:
                        with self.assertRaises(MODULE.SyncError):
                            MODULE._cleanup_ready_pending_batches(self.home)
                        self.assertTrue(ticket.path.is_file())
                finally:
                    self.home = original_home

    def test_partial_canonical_cleanup_authority_is_never_overwritten(self) -> None:
        for version, label in ((2, "rollback"), (3, "staging")):
            with self.subTest(label=label):
                case_home = self.root / f"partial-ticket-home-{label}"
                case_home.mkdir()
                original_home = self.home
                self.home = case_home
                try:
                    batch_root = MODULE._quarantine_batch_root(self.home, [])
                    payload = self._legacy_cleanup_ticket_payload(
                        batch_root,
                        version=version,
                    )
                    ticket_path = MODULE._pending_cleanup_ticket_path(
                        self.home,
                        batch_root.name,
                    )
                    ticket_path.parent.mkdir(parents=True, exist_ok=True)
                    partial = payload[: max(1, len(payload) // 2)]
                    ticket_path.write_bytes(partial)
                    ticket_path.chmod(0o600)

                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "appeared with changed content",
                    ):
                        MODULE._publish_pending_cleanup_ticket(
                            self.home,
                            ticket_path,
                            payload,
                        )
                    self.assertEqual(ticket_path.read_bytes(), partial)
                finally:
                    self.home = original_home

        ticket = self._publish_legacy_cleanup_ticket(version=3)
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        proof_path.write_bytes(b"{\n")
        proof_path.chmod(0o600)
        with self.assertRaisesRegex(MODULE.SyncError, "empty proof changed"):
            MODULE._publish_pending_cleanup_empty_proof(
                self.home,
                ticket,
                (quarantine_root.stat().st_dev, quarantine_root.stat().st_ino),
            )
        self.assertEqual(proof_path.read_bytes(), b"{\n")

    def test_cleanup_authority_readers_reject_foreign_owner_uid(self) -> None:
        foreign_uid = os.geteuid() + 1
        ticket = self._publish_legacy_cleanup_ticket(version=3)
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_root_identity = (
            quarantine_root.stat().st_dev,
            quarantine_root.stat().st_ino,
        )
        proof = MODULE._publish_pending_cleanup_empty_proof(
            self.home,
            ticket,
            quarantine_root_identity,
        )

        staging_root = MODULE._quarantine_batch_root(self.home, [])
        staging_identity = (
            staging_root.stat().st_dev,
            staging_root.stat().st_ino,
        )
        staging_path = staging_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        marker = MODULE._write_exclusive_internal_file(
            self.home,
            staging_path,
            MODULE._pending_staging_marker_payload(
                staging_root,
                staging_identity,
            ),
        )
        self.assertEqual(ticket.snapshot.uid, os.geteuid())
        self.assertEqual(proof.uid, os.geteuid())
        self.assertEqual(marker.uid, os.geteuid())

        with mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket owner changed",
            ):
                MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup empty proof changed",
            ):
                MODULE._read_pending_cleanup_empty_proof(
                    self.home,
                    ticket,
                    quarantine_root_identity,
                )
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending staging cleanup marker changed",
            ):
                MODULE._pending_staging_marker_snapshot(
                    self.home,
                    staging_root,
                    staging_identity,
                )

    def test_pending_cleanup_linux_fd_policy_requires_owner_and_exact_mode(
        self,
    ) -> None:
        control_file = self.root / "pending-control.json"
        control_file.write_bytes(b"{}\n")
        control_file.chmod(0o600)
        control_directory = self.root / "pending-control-directory"
        control_directory.mkdir(mode=0o700)
        foreign_uid = os.geteuid() + 1

        for path, expected_mode, unsafe_mode, open_flags in (
            (control_file, 0o600, 0o640, os.O_RDONLY),
            (
                control_directory,
                0o700,
                0o750,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            ),
        ):
            with self.subTest(path=path.name):
                file_descriptor = os.open(path, open_flags)
                try:
                    with mock.patch.object(MODULE.sys, "platform", "linux"):
                        metadata = MODULE._require_pending_cleanup_fd_access_policy(
                            file_descriptor,
                            path,
                            expected_mode=expected_mode,
                        )
                        self.assertEqual(metadata.st_uid, os.geteuid())
                        generic_metadata = (
                            MODULE._require_release_identity_fd_access_policy(
                                file_descriptor,
                                path,
                                foreign_uid,
                            )
                        )
                        self.assertEqual(generic_metadata.st_uid, os.geteuid())
                        with (
                            mock.patch.object(
                                MODULE.os,
                                "geteuid",
                                return_value=foreign_uid,
                            ),
                            self.assertRaisesRegex(
                                MODULE.SyncError,
                                "owner UID",
                            ),
                        ):
                            MODULE._require_pending_cleanup_fd_access_policy(
                                file_descriptor,
                                path,
                                expected_mode=expected_mode,
                            )
                        path.chmod(unsafe_mode)
                        with self.assertRaisesRegex(
                            MODULE.SyncError,
                            f"mode {unsafe_mode:04o} != {expected_mode:04o}",
                        ):
                            MODULE._require_pending_cleanup_fd_access_policy(
                                file_descriptor,
                                path,
                                expected_mode=expected_mode,
                            )
                finally:
                    os.close(file_descriptor)

    def test_pending_cleanup_accepts_links_content_directories_mode_0755(
        self,
    ) -> None:
        ticket = self._publish_legacy_cleanup_ticket(version=2)
        links_parent = ticket.batch_root / "links"
        content_parent = links_parent / "agents"
        content_parent.mkdir(parents=True, mode=0o755)
        links_parent.chmod(0o755)
        content_parent.chmod(0o755)
        content = content_parent / "role.toml"
        content.write_bytes(b'name = "retained"\n')
        content.chmod(0o644)

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())

    def test_pending_cleanup_accepts_typed_active_links_regular_file_mode_0644(
        self,
    ) -> None:
        ticket = self._publish_legacy_cleanup_ticket(version=2)
        content = ticket.batch_root / "links"
        content.write_bytes(b'name = "retained"\n')
        content.chmod(0o644)
        batch_fd = os.open(
            ticket.batch_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            metadata = content.stat()
            active_name, _active_metadata = MODULE._isolate_pending_cleanup_entry(
                batch_fd,
                content.name,
                MODULE._directory_identity(batch_fd),
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    MODULE.stat.S_IFREG,
                ),
                links_content_root=True,
            )
        finally:
            os.close(batch_fd)
        active = ticket.batch_root / active_name
        self.assertEqual(MODULE.stat.S_IMODE(active.stat().st_mode), 0o644)

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())

    def test_pending_cleanup_recovers_active_links_content_after_restart(
        self,
    ) -> None:
        ticket = self._publish_legacy_cleanup_ticket(version=2)
        links_parent = ticket.batch_root / "links"
        content_parent = links_parent / "agents"
        content_parent.mkdir(parents=True, mode=0o755)
        links_parent.chmod(0o755)
        content_parent.chmod(0o755)
        content = content_parent / "role.toml"
        content.write_bytes(b'name = "retained"\n')
        content.chmod(0o600)
        real_isolate = MODULE._isolate_pending_cleanup_entry
        tripped = False

        def isolate_then_fail(*args, **kwargs):
            nonlocal tripped
            active = real_isolate(*args, **kwargs)
            if not tripped and args[1] == "links":
                tripped = True
                raise MODULE.SyncError("injected crash after links isolation")
            return active

        with (
            mock.patch.object(
                MODULE,
                "_isolate_pending_cleanup_entry",
                side_effect=isolate_then_fail,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected crash after links isolation",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        active_links = list(
            ticket.batch_root.glob(
                f"{MODULE.PENDING_CLEANUP_ACTIVE_LINKS_ENTRY_PREFIX}*"
            )
        )
        self.assertEqual(len(active_links), 1)
        self.assertEqual(active_links[0].stat().st_mode & 0o777, 0o755)
        self.assertTrue(ticket.path.is_file())

        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))

        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())

    def test_pending_cleanup_active_control_directory_stays_exact_0700(
        self,
    ) -> None:
        ticket = self._publish_legacy_cleanup_ticket(version=2)
        state_directory = ticket.batch_root / "state"
        state_directory.mkdir(mode=0o700)
        control_directory = state_directory / "extra-control"
        control_directory.mkdir(mode=0o700)
        state_fd = os.open(
            state_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            metadata = control_directory.stat()
            active_name, _active_metadata = MODULE._isolate_pending_cleanup_entry(
                state_fd,
                control_directory.name,
                MODULE._directory_identity(state_fd),
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    MODULE.stat.S_IFDIR,
                ),
            )
        finally:
            os.close(state_fd)
        active_control = state_directory / active_name
        active_control.chmod(0o755)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            rf"<pending-cleanup-scan>/state/{active_name}: mode 0755 != 0700",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(active_control.is_dir())
        self.assertTrue(ticket.path.is_file())

    def test_pending_cleanup_legacy_active_links_token_fails_closed(
        self,
    ) -> None:
        ticket = self._publish_legacy_cleanup_ticket(version=2)
        links_directory = ticket.batch_root / "links"
        links_directory.mkdir(mode=0o700)
        batch_fd = os.open(
            ticket.batch_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            metadata = links_directory.stat()
            active_name, _active_metadata = MODULE._isolate_pending_cleanup_entry(
                batch_fd,
                links_directory.name,
                MODULE._directory_identity(batch_fd),
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    MODULE.stat.S_IFDIR,
                ),
            )
        finally:
            os.close(batch_fd)
        legacy_active_links = ticket.batch_root / active_name
        legacy_active_links.chmod(0o755)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            rf"<pending-cleanup-scan>/{active_name}: mode 0755 != 0700",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(legacy_active_links.is_dir())
        self.assertTrue(ticket.path.is_file())

    def test_pending_cleanup_rejects_control_directory_mode_0755(self) -> None:
        ticket = self._publish_legacy_cleanup_ticket(version=2)
        control_directory = ticket.batch_root / "pending"
        self.assertEqual(control_directory.stat().st_mode & 0o777, 0o700)
        control_directory.chmod(0o755)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            r"<pending-cleanup-scan>/pending: mode 0755 != 0700",
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue(ticket.path.is_file())
        self.assertTrue(ticket.batch_root.is_dir())

    def test_orphan_empty_proof_rejects_foreign_owner_before_and_after_retention(
        self,
    ) -> None:
        ticket = self._publish_legacy_cleanup_ticket(version=3)
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_root_identity = (
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
            quarantine_root_identity,
        )
        with mock.patch.object(
            MODULE,
            "_delete_pending_cleanup_empty_proof",
            return_value=None,
        ):
            self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(ticket.batch_root.exists())
        self.assertFalse(ticket.path.exists())
        self.assertTrue(proof_path.is_file())

        foreign_uid = os.geteuid() + 1
        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup empty proof changed",
            ),
        ):
            MODULE._read_orphan_pending_cleanup_empty_proof(
                self.home,
                proof_path,
            )

        retained_name = next(MODULE._retained_pending_cleanup_names(proof_path))
        retained_path = proof_path.with_name(retained_name)
        proof_path.rename(retained_path)
        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup retained control owner changed",
            ),
        ):
            MODULE._restore_pending_cleanup_control_tombstones(self.home)
        self.assertFalse(proof_path.exists())
        self.assertTrue(retained_path.is_file())

    def test_atomic_authority_publisher_rejects_foreign_owner_uid(self) -> None:
        authority_root = self.home / "atomic-authority-owner-tests"
        authority_root.mkdir()
        payload = b'{"authority": true}\n'
        foreign_uid = os.geteuid() + 1

        existing_path = authority_root / "existing.json"
        existing_path.write_bytes(payload)
        existing_path.chmod(0o600)
        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "atomic internal authority is incomplete or changed",
            ),
        ):
            MODULE._publish_atomic_exclusive_internal_file(
                self.home,
                existing_path,
                payload,
            )

        raced_path = authority_root / "raced.json"

        def publish_foreign_owner_race(*_args) -> None:
            raced_path.write_bytes(payload)
            raced_path.chmod(0o600)
            raise FileExistsError("injected authority publication race")

        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=publish_foreign_owner_race,
            ),
        ):
            self._assert_internal_publication_cleanup_failure(
                lambda: MODULE._publish_atomic_exclusive_internal_file(
                    self.home,
                    raced_path,
                    payload,
                ),
                publication_failure=(
                    "atomic internal authority appeared with changed content"
                ),
            )

    def test_cleanup_ticket_file_exists_race_rejects_foreign_owner_uid(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            "20260901T000000Z-12-0",
        )
        payload = b'{"ticket": true}\n'
        foreign_uid = os.geteuid() + 1

        def publish_foreign_owner_race(*_args) -> None:
            ticket_path.write_bytes(payload)
            ticket_path.chmod(0o600)
            raise FileExistsError("injected cleanup ticket publication race")

        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=publish_foreign_owner_race,
            ),
        ):
            self._assert_internal_publication_cleanup_failure(
                lambda: MODULE._publish_pending_cleanup_ticket(
                    self.home,
                    ticket_path,
                    payload,
                ),
                publication_failure=(
                    "pending cleanup ticket appeared with changed content"
                ),
            )

    def test_ticket_temp_publisher_recovers_truncated_rollback_and_staging_temps(
        self,
    ) -> None:
        for version, label in ((2, "rollback"), (3, "staging")):
            with self.subTest(label=label):
                case_home = self.root / f"temp-ticket-home-{label}"
                case_home.mkdir()
                original_home = self.home
                self.home = case_home
                try:
                    batch_root = MODULE._quarantine_batch_root(self.home, [])
                    payload = self._legacy_cleanup_ticket_payload(
                        batch_root,
                        version=version,
                    )
                    index_fd = MODULE._open_or_create_directory_beneath(
                        self.home,
                        MODULE._pending_cleanup_index_path(self.home),
                        mode=0o700,
                    )
                    MODULE._close_fd_quietly(index_fd)
                    ticket_path = MODULE._pending_cleanup_ticket_path(
                        self.home,
                        batch_root.name,
                    )
                    temp_path = ticket_path.with_name(
                        batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
                    )
                    temp_path.write_bytes(payload[:1])
                    temp_path.chmod(0o600)

                    MODULE._publish_pending_cleanup_ticket(
                        self.home,
                        ticket_path,
                        payload,
                    )

                    self.assertEqual(ticket_path.read_bytes(), payload)
                    self.assertFalse(temp_path.exists())
                finally:
                    self.home = original_home

    def test_ticket_temp_rechecks_tolerate_mode_0600_gid_churn(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        payload = b'{"ticket": true}\n'
        real_write = MODULE._write_exclusive_internal_file

        def write_then_churn_gid(
            home: Path,
            path: Path,
            written_payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            staged = real_write(home, path, written_payload)
            assert staged.gid is not None
            os.chown(path, -1, self._alternate_gid(staged.gid))
            return staged

        for batch_name, preexisting in (
            ("20260901T000000Z-10-0", False),
            ("20260901T000000Z-10-1", True),
        ):
            with self.subTest(preexisting=preexisting):
                ticket_path = MODULE._pending_cleanup_ticket_path(
                    self.home,
                    batch_name,
                )
                if preexisting:
                    ticket_path.write_bytes(payload)
                    ticket_path.chmod(0o600)
                with mock.patch.object(
                    MODULE,
                    "_write_exclusive_internal_file",
                    side_effect=write_then_churn_gid,
                ):
                    published = MODULE._publish_pending_cleanup_ticket(
                        self.home,
                        ticket_path,
                        payload,
                    )

                self.assertEqual(published.payload, payload)
                self.assertEqual(ticket_path.read_bytes(), payload)
                self.assertFalse(
                    ticket_path.with_name(ticket_path.name + ".tmp").exists()
                )

    def test_ticket_rechecks_and_cleanup_tolerate_mode_0600_gid_churn(
        self,
    ) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        payload = self._legacy_cleanup_ticket_payload(batch_root, version=3)
        published_before_churn: MODULE.ManagedStateFileSnapshot | None = None
        real_publish = MODULE._publish_pending_cleanup_ticket

        def publish_then_churn_gid(
            home: Path,
            ticket_path: Path,
            ticket_payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal published_before_churn
            published_before_churn = real_publish(
                home,
                ticket_path,
                ticket_payload,
            )
            assert published_before_churn.gid is not None
            os.chown(
                ticket_path,
                -1,
                self._alternate_gid(published_before_churn.gid),
            )
            return published_before_churn

        with mock.patch.object(
            MODULE,
            "_publish_pending_cleanup_ticket",
            side_effect=publish_then_churn_gid,
        ):
            MODULE._publish_pending_batch_cleanup_ticket_for_root(
                self.home,
                batch_root,
                payload,
            )

        self.assertIsNotNone(published_before_churn)
        assert published_before_churn is not None
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        current_ticket = MODULE._read_pending_cleanup_ticket(
            self.home,
            ticket_path,
        )
        self.assertIsNotNone(current_ticket)
        assert current_ticket is not None
        self.assertNotEqual(
            current_ticket.snapshot.gid,
            published_before_churn.gid,
        )
        expected_ticket = MODULE.replace(
            current_ticket,
            snapshot=published_before_churn,
        )

        MODULE._verify_pending_cleanup_ticket_durable(
            self.home,
            expected_ticket,
        )
        self.assertTrue(
            MODULE._remove_cleanup_ready_batch(
                self.home,
                expected_ticket,
            )
        )

        self.assertFalse(ticket_path.exists())
        self.assertFalse(batch_root.exists())

    def test_cursor_writer_tolerates_mode_0600_temp_gid_churn(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_name = "20260901T000000Z-11-0.json"
        real_write = MODULE._write_exclusive_internal_file
        alternate_gid: int | None = None

        def write_then_churn_gid(
            home: Path,
            path: Path,
            payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal alternate_gid
            staged = real_write(home, path, payload)
            assert staged.gid is not None
            alternate_gid = self._alternate_gid(staged.gid)
            os.chown(path, -1, alternate_gid)
            return staged

        with mock.patch.object(
            MODULE,
            "_write_exclusive_internal_file",
            side_effect=write_then_churn_gid,
        ):
            self.assertEqual(
                MODULE._write_pending_cleanup_cursor(
                    self.home,
                    index_root,
                    ticket_name,
                ),
                0,
            )

        cursor_path = index_root / MODULE.PENDING_CLEANUP_CURSOR_NAME
        self.assertEqual(
            MODULE._read_pending_cleanup_cursor(self.home, index_root),
            ticket_name,
        )
        self.assertEqual(cursor_path.stat().st_gid, alternate_gid)

    def test_retained_ticket_temp_cleanup_is_bounded_per_run(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        retained_paths: list[Path] = []
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN + 2):
            batch_name = f"20260901T000000Z-1-{index}"
            retained = index_root / (
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{batch_name}"
                f"{MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX}-123-"
                f"{index:016x}"
            )
            retained.write_bytes(b"temporary\n")
            retained.chmod(0o600)
            retained_paths.append(retained)

        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(self.home),
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(sum(path.exists() for path in retained_paths), 2)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 2)
        self.assertFalse(any(path.exists() for path in retained_paths))
        self.assertFalse(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

    def test_cursor_temp_residue_uses_shared_budget_before_ticket_selection(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        tickets = {}
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN):
            batch_name = f"20260901T000000Z-2-{index}"
            ticket_path = index_root / (
                batch_name + MODULE.PENDING_CLEANUP_TICKET_SUFFIX
            )
            ticket_path.write_bytes(b"{}\n")
            ticket_path.chmod(0o600)
            tickets[ticket_path.name] = mock.Mock(version=3)
        retained_cursor = index_root / (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
            f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
            "0000000000000001"
        )
        retained_cursor.write_bytes(b"cursor\n")
        retained_cursor.chmod(0o600)

        def read_ticket(_home: Path, path: Path, **_kwargs):
            return tickets.get(path.name)

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                side_effect=read_ticket,
            ),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                return_value=True,
            ) as remove_batch,
        ):
            self.assertEqual(
                MODULE._cleanup_ready_pending_batches(self.home),
                MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
            )

        self.assertEqual(
            remove_batch.call_count,
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN - 1,
        )
        self.assertFalse(retained_cursor.exists())
        self.assertTrue((index_root / MODULE.PENDING_CLEANUP_CURSOR_NAME).is_file())

    def test_cursor_progress_reaches_ninth_v4_after_deferred_prefix(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        terminal_ticket = self._only_cleanup_ticket()
        self.assertEqual(terminal_ticket.version, 4)
        index_root = MODULE._pending_cleanup_index_path(self.home)
        deferred_tickets = {}
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN):
            batch_name = f"20000101T000000Z-1-{index}"
            ticket_path = index_root / (
                batch_name + MODULE.PENDING_CLEANUP_TICKET_SUFFIX
            )
            ticket_path.write_bytes(b"{}\n")
            ticket_path.chmod(0o600)
            deferred_tickets[ticket_path.name] = mock.Mock(version=3)
        retained_cursor = index_root / (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
            f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
            "0000000000000003"
        )
        retained_cursor.write_bytes(b"cursor\n")
        retained_cursor.chmod(0o600)
        real_read = MODULE._read_pending_cleanup_ticket
        real_remove = MODULE._remove_cleanup_ready_batch
        real_verify = MODULE._verify_final_regular_targets

        def read_ticket(home: Path, path: Path, **kwargs):
            deferred = deferred_tickets.get(path.name)
            if deferred is not None:
                return deferred
            return real_read(home, path, **kwargs)

        def defer_prefix(home: Path, ticket):
            if ticket in deferred_tickets.values():
                raise MODULE.SyncError("injected persistent deferred cleanup")
            return real_remove(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                side_effect=read_ticket,
            ),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=defer_prefix,
            ),
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                wraps=real_verify,
            ) as verify,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        verify.assert_called_once()
        self.assertFalse(retained_cursor.exists())
        self.assertFalse(terminal_ticket.path.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_cursor_temp_forms_are_observed(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        cursor_temp = index_root / MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME
        cursor_temp.write_bytes(b"cursor\n")
        cursor_temp.chmod(0o600)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

        cursor_temp.unlink()
        retained_cursor = index_root / (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
            f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
            "0000000000000002"
        )
        retained_cursor.write_bytes(b"cursor\n")
        retained_cursor.chmod(0o600)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

    def test_cursor_writer_skips_publication_while_residue_remains(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        retained_paths: list[Path] = []
        for index in range(2):
            retained = index_root / (
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
                f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
                f"{index:016x}"
            )
            retained.write_bytes(b"cursor\n")
            retained.chmod(0o600)
            retained_paths.append(retained)

        self.assertEqual(
            MODULE._write_pending_cleanup_cursor(
                self.home,
                index_root,
                "20260901T000000Z-5-0.json",
                max_temp_cleanup_actions=1,
            ),
            1,
        )

        self.assertEqual(sum(path.exists() for path in retained_paths), 1)
        self.assertFalse((index_root / MODULE.PENDING_CLEANUP_CURSOR_NAME).exists())

    def test_restored_v4_ticket_is_finalized_after_budget_is_charged(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 4)
        retained_ticket = ticket.path.with_name(
            next(MODULE._retained_pending_cleanup_names(ticket.path))
        )
        ticket.path.rename(retained_ticket)
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN - 1):
            batch_name = f"20260901T000000Z-3-{index}"
            temp_path = ticket.path.parent / (
                batch_name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
            )
            temp_path.write_bytes(b"temporary\n")
            temp_path.chmod(0o600)

        real_verify = MODULE._verify_final_regular_targets
        with mock.patch.object(
            MODULE,
            "_verify_final_regular_targets",
            wraps=real_verify,
        ) as verify:
            self.assertEqual(
                MODULE._cleanup_ready_pending_batches(self.home),
                MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
            )

        verify.assert_called_once()
        self.assertFalse(ticket.path.exists())
        self.assertFalse(retained_ticket.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_retained_control_ambiguity_is_checked_beyond_selected_slice(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        retained_paths: list[Path] = []
        for batch_index, retained_count in ((0, 1), (1, 2)):
            canonical = (
                f"20260901T000000Z-4-{batch_index}"
                f"{MODULE.PENDING_CLEANUP_TICKET_SUFFIX}"
            )
            for retained_index in range(retained_count):
                retained = index_root / (
                    f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{canonical}-123-"
                    f"{retained_index:016x}"
                )
                retained.write_bytes(b"retained\n")
                retained.chmod(0o600)
                retained_paths.append(retained)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "multiple retained files",
        ):
            MODULE._restore_pending_cleanup_control_tombstones(self.home, limit=1)

        self.assertTrue(all(path.is_file() for path in retained_paths))

    def test_top_level_install_reuses_one_cleanup_budget_across_passes(
        self,
    ) -> None:
        release_expectation = MODULE._source_release_identity(
            self.next_release,
            None,
        )
        release = mock.Mock(
            release_root=self.next_release,
            release_expectation=release_expectation,
        )
        release.assets.sha = SHA_B
        workspace = mock.Mock()
        workspace.path = self.root / "download-workspace"
        seen_budgets: list[MODULE.PendingCleanupActionBudget] = []
        phases: list[str] = []

        def preflight(
            _home: Path,
            *,
            dry_run: bool,
            cleanup_budget: MODULE.PendingCleanupActionBudget,
        ) -> bool:
            self.assertFalse(dry_run)
            phases.append("preflight")
            seen_budgets.append(cleanup_budget)
            cleanup_budget.consume_control_actions(2)
            return False

        def install_set(
            _home: Path,
            _releases,
            *,
            dry_run: bool,
            preflight_only: bool = False,
            cleanup_budget: MODULE.PendingCleanupActionBudget,
            **_kwargs,
        ) -> None:
            phases.append(
                "dry preflight" if dry_run and preflight_only else "locked install"
            )
            seen_budgets.append(cleanup_budget)
            cleanup_budget.consume_control_actions(2)

        with (
            mock.patch.object(
                MODULE,
                "temporary_archive_workspace",
                return_value=contextlib.nullcontext(workspace),
            ),
            mock.patch.object(
                MODULE,
                "download_and_extract_release",
                return_value=release,
            ),
            mock.patch.object(
                MODULE,
                "_preflight_pending_recovery",
                side_effect=preflight,
            ),
            mock.patch.object(
                MODULE,
                "_install_release_set_unlocked",
                side_effect=install_set,
            ),
            mock.patch.object(
                MODULE,
                "installation_lock",
                return_value=contextlib.nullcontext(),
            ),
        ):
            MODULE.install_from_github("Joey-Tools/example", self.home, dry_run=False)

        self.assertEqual(
            phases,
            ["preflight", "preflight", "dry preflight", "locked install"],
        )
        self.assertTrue(all(budget is seen_budgets[0] for budget in seen_budgets))
        self.assertEqual(
            seen_budgets[0].limit,
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(
            seen_budgets[0].consumed,
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(seen_budgets[0].remaining, 0)

    def test_shared_cleanup_helper_reports_zero_delta_after_budget_is_spent(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN):
            batch_name = f"20260901T000000Z-6-{index}"
            retained = index_root / (
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{batch_name}"
                f"{MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX}-123-"
                f"{index:016x}"
            )
            retained.write_bytes(b"temporary\n")
            retained.chmod(0o600)

        budget = MODULE.PendingCleanupActionBudget(
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN
        )
        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(self.home, budget=budget),
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(budget.remaining, 0)
        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(self.home, budget=budget),
            0,
        )

    def test_orphan_empty_proof_scan_skips_paired_prefix_before_budget(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN + 1):
            batch_name = f"20000101T000000Z-1-{index}"
            (index_root / f"{batch_name}.empty-proof").write_bytes(b"paired proof\n")
            (index_root / f"{batch_name}.json").write_bytes(b"paired ticket\n")
        orphan_batch = "20990101T000000Z-1-0"
        orphan_path = index_root / f"{orphan_batch}.empty-proof"
        orphan_path.write_bytes(b"orphan proof\n")
        proof = mock.Mock()

        def isolate(
            _home: Path,
            path: Path,
            _parent_fd: int,
            _expected,
            *,
            label: str,
            mutation_revalidator=None,
        ) -> None:
            self.assertEqual(path, orphan_path)
            self.assertIn(orphan_batch, label)
            self.assertIsNotNone(mutation_revalidator)
            path.unlink()

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=None,
            ) as read_ticket,
            mock.patch.object(
                MODULE,
                "_read_orphan_pending_cleanup_empty_proof",
                return_value=proof,
            ) as read_proof,
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=isolate,
            ) as delete_proof,
        ):
            self.assertEqual(
                MODULE._cleanup_orphan_pending_cleanup_empty_proofs(
                    self.home,
                    limit=1,
                ),
                1,
            )

        read_ticket.assert_called_once_with(
            self.home,
            index_root / f"{orphan_batch}.json",
        )
        read_proof.assert_called_once_with(self.home, orphan_path)
        delete_proof.assert_called_once()
        self.assertFalse(orphan_path.exists())

    def test_orphan_empty_proof_budget_is_charged_before_content_reads(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        batch_name = "20260901T000000Z-7-0"
        proof_path = index_root / f"{batch_name}.empty-proof"
        proof_path.write_bytes(b"not read\n")

        for limit in (0, 1):
            with self.subTest(limit=limit):
                budget = MODULE.PendingCleanupActionBudget(limit)
                ticket = None if limit == 0 else mock.Mock()
                with (
                    mock.patch.object(
                        MODULE,
                        "_read_pending_cleanup_ticket",
                        return_value=ticket,
                    ) as read_ticket,
                    mock.patch.object(
                        MODULE,
                        "_read_orphan_pending_cleanup_empty_proof",
                    ) as read_proof,
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                    ) as delete_proof,
                ):
                    self.assertEqual(
                        MODULE._cleanup_orphan_pending_cleanup_empty_proofs(
                            self.home,
                            budget=budget,
                        ),
                        0,
                    )

                if limit == 0:
                    read_ticket.assert_not_called()
                    self.assertEqual(budget.consumed, 0)
                else:
                    read_ticket.assert_called_once()
                    self.assertEqual(budget.consumed, 1)
                read_proof.assert_not_called()
                delete_proof.assert_not_called()

    def test_staging_initial_skeleton_uses_fixed_structural_scan_bounds(
        self,
    ) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        for relative_path in (
            Path("pending/before"),
            Path("pending/stage"),
            Path("pending/evidence"),
            Path("pending/state"),
            Path("pending/cleanup"),
            Path("pending/claims/before"),
            Path("pending/claims/after"),
        ):
            directory_fd = MODULE._open_or_create_directory_beneath(
                self.home,
                batch_root / relative_path,
                mode=0o700,
            )
            MODULE._close_fd_quietly(directory_fd)
        marker_path = batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
        marker_temp = marker_path.with_name(
            marker_path.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        )
        marker_temp.write_bytes(b"staging marker temp\n")
        marker_temp.chmod(0o600)
        maximum_entries: list[int | None] = []
        real_member_names = MODULE._directory_member_names

        def bounded_member_names(directory_fd: int, **kwargs):
            maximum_entries.append(kwargs.get("maximum_entries"))
            return real_member_names(directory_fd, **kwargs)

        with mock.patch.object(
            MODULE,
            "_directory_member_names",
            side_effect=bounded_member_names,
        ):
            MODULE._require_pending_staging_initial_skeleton(
                self.home,
                batch_root,
                (batch_root.stat().st_dev, batch_root.stat().st_ino),
            )

        self.assertEqual(
            maximum_entries,
            [3, 7, 3, 1, 1, 1, 1, 1, 1, 2],
        )

    def test_staging_temp_recovery_rejects_unsafe_skeleton_modes(self) -> None:
        for relative_path, unsafe_mode in (
            (Path("."), 0o755),
            (Path("pending"), 0o755),
            (Path("pending/claims/after"), 0o755),
            (Path("metadata.json"), 0o644),
            (None, 0o644),
        ):
            with self.subTest(relative_path=relative_path):
                batch_root = MODULE._quarantine_batch_root(self.home, [])
                for skeleton_path in (
                    Path("pending/before"),
                    Path("pending/stage"),
                    Path("pending/evidence"),
                    Path("pending/state"),
                    Path("pending/cleanup"),
                    Path("pending/claims/before"),
                    Path("pending/claims/after"),
                ):
                    directory_fd = MODULE._open_or_create_directory_beneath(
                        self.home,
                        batch_root / skeleton_path,
                        mode=0o700,
                    )
                    MODULE._close_fd_quietly(directory_fd)
                marker_path = batch_root / Path(
                    *MODULE.PENDING_STATE_STAGING_MARKER.parts
                )
                temp_path = marker_path.with_name(
                    marker_path.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
                )
                temp_path.write_bytes(b"{\n")
                temp_path.chmod(0o600)
                unsafe_path = (
                    temp_path if relative_path is None else batch_root / relative_path
                )
                unsafe_path.chmod(unsafe_mode)

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending staging (directory owner, mode, or identity|metadata|temp authority)",
                ):
                    MODULE._require_pending_staging_initial_skeleton(
                        self.home,
                        batch_root,
                        (batch_root.stat().st_dev, batch_root.stat().st_ino),
                    )

                self.assertTrue(temp_path.exists())
                self.assertFalse(marker_path.exists())
                self.assertTrue(batch_root.exists())

    def test_staging_temp_recovery_rejects_foreign_owned_temp_evidence(
        self,
    ) -> None:
        for retained in (False, True):
            with self.subTest(retained=retained):
                batch_root = MODULE._quarantine_batch_root(self.home, [])
                for relative_path in (
                    Path("pending/before"),
                    Path("pending/stage"),
                    Path("pending/evidence"),
                    Path("pending/state"),
                    Path("pending/cleanup"),
                    Path("pending/claims/before"),
                    Path("pending/claims/after"),
                ):
                    directory_fd = MODULE._open_or_create_directory_beneath(
                        self.home,
                        batch_root / relative_path,
                        mode=0o700,
                    )
                    MODULE._close_fd_quietly(directory_fd)
                marker_path = batch_root / Path(
                    *MODULE.PENDING_STATE_STAGING_MARKER.parts
                )
                temp_path = marker_path.with_name(
                    marker_path.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
                )
                temp_path.write_bytes(b"{\n")
                temp_path.chmod(0o600)
                observed_temp = temp_path
                if retained:
                    observed_temp = temp_path.with_name(
                        next(MODULE._retained_pending_cleanup_names(temp_path))
                    )
                    temp_path.rename(observed_temp)
                real_read = MODULE._read_managed_state_file_snapshot

                def read_with_foreign_temp_owner(home, path, parent_fd, **kwargs):
                    snapshot = real_read(home, path, parent_fd, **kwargs)
                    if path == observed_temp and snapshot.exists:
                        return MODULE.replace(
                            snapshot,
                            uid=os.geteuid() + 1,
                        )
                    return snapshot

                with (
                    mock.patch.object(
                        MODULE,
                        "_read_managed_state_file_snapshot",
                        side_effect=read_with_foreign_temp_owner,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "pending staging temp authority owner or mode changed",
                    ),
                ):
                    MODULE._recover_pending_staging_marker_temps(
                        self.home,
                        budget=MODULE.PendingCleanupActionBudget(2),
                    )

                self.assertTrue(observed_temp.exists())
                self.assertFalse(marker_path.exists())
                self.assertTrue(batch_root.exists())

    def test_incomplete_temp_discard_rejects_foreign_owner_uid(self) -> None:
        temp_root = self.home / "foreign-temp-discard"
        temp_root.mkdir(mode=0o700)
        temp_path = temp_root / "authority.json.tmp"
        temp_path.write_bytes(b"{\n")
        temp_path.chmod(0o600)
        real_read = MODULE._read_managed_state_file_snapshot

        def read_with_foreign_owner(home, path, parent_fd, **kwargs):
            snapshot = real_read(home, path, parent_fd, **kwargs)
            if path == temp_path and snapshot.exists:
                return MODULE.replace(snapshot, uid=os.geteuid() + 1)
            return snapshot

        with (
            mock.patch.object(
                MODULE,
                "_read_managed_state_file_snapshot",
                side_effect=read_with_foreign_owner,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "incomplete pending cleanup ticket changed",
            ),
        ):
            MODULE._discard_incomplete_pending_cleanup_ticket(
                self.home,
                temp_path,
            )

        self.assertTrue(temp_path.exists())
        self.assertEqual(temp_path.read_bytes(), b"{\n")

    def test_cursor_temp_failures_do_not_hold_index_fd(self) -> None:
        for label, cleanup_result, budget_limit in (
            ("cleanup", MODULE.SyncError("injected cursor cleanup failure"), 1),
            ("consume", 2, 1),
        ):
            with self.subTest(failure=label):
                with (
                    mock.patch.object(
                        MODULE,
                        "_pending_link_pointer_is_absent",
                        return_value=True,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_require_no_pending_unresolved_ticket_representations",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_restore_pending_cleanup_control_tombstones",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_pending_cleanup_ticket_temps",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_pending_private_use_retirements",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_publish_discovered_staging_cleanup_tickets",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_orphan_pending_cleanup_empty_proofs",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_orphan_pending_ephemeral_private_phases",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_pending_cleanup_cursor_temp",
                        side_effect=(
                            cleanup_result
                            if isinstance(cleanup_result, BaseException)
                            else None
                        ),
                        return_value=(
                            cleanup_result
                            if isinstance(cleanup_result, int)
                            else mock.DEFAULT
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_open_directory_beneath",
                        return_value=123,
                    ) as open_index,
                    mock.patch.object(MODULE, "_close_fd_quietly") as close_index,
                    self.assertRaises(MODULE.SyncError),
                ):
                    MODULE._cleanup_ready_pending_batches(
                        self.home,
                        budget=MODULE.PendingCleanupActionBudget(budget_limit),
                    )
                open_index.assert_called_once_with(
                    self.home,
                    MODULE._pending_cleanup_index_path(self.home),
                )
                close_index.assert_called_once_with(123)

    def test_cleanup_ready_batches_returns_zero_when_index_is_absent(self) -> None:
        fresh_home = self.root / "fresh-home"
        fresh_home.mkdir()
        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(fresh_home),
            0,
        )

    def test_exhausted_budget_blocks_v3_v4_authority_before_new_mutation(
        self,
    ) -> None:
        release_expectation = MODULE._source_release_identity(
            self.next_release,
            None,
        )
        manifest = release_expectation[0][1]
        releases = [(self.next_release, SHA_B, manifest, release_expectation)]

        for version in (3, 4):
            with self.subTest(ticket_version=version):
                case_home = self.root / f"terminal-authority-home-v{version}"
                write_release(
                    case_home / "first-release", role_payload='name = "first"\n'
                )
                install(case_home / "first-release", case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                case_state_path = MODULE._state_path(case_home)
                target_before = case_target.read_bytes()
                state_before = case_state_path.read_bytes()
                original_home = self.home
                self.home = case_home
                try:
                    if version == 3:
                        batch_root = MODULE._quarantine_batch_root(self.home, [])
                        batch_identity = (
                            batch_root.stat().st_dev,
                            batch_root.stat().st_ino,
                        )
                        marker_path = batch_root / Path(
                            *MODULE.PENDING_STATE_STAGING_MARKER.parts
                        )
                        marker_path.parent.mkdir(parents=True, exist_ok=True)
                        marker = MODULE._write_exclusive_internal_file(
                            self.home,
                            marker_path,
                            MODULE._pending_staging_marker_payload(
                                batch_root,
                                batch_identity,
                            ),
                        )
                        ticket_path = MODULE._pending_cleanup_ticket_path(
                            self.home,
                            batch_root.name,
                        )
                        index_fd = MODULE._open_or_create_directory_beneath(
                            self.home,
                            ticket_path.parent,
                            mode=0o700,
                        )
                        MODULE._close_fd_quietly(index_fd)
                        MODULE._publish_pending_cleanup_ticket(
                            self.home,
                            ticket_path,
                            MODULE._pending_staging_cleanup_ticket_payload(
                                batch_root,
                                batch_identity,
                                marker,
                            ),
                        )
                        ticket = MODULE._read_pending_cleanup_ticket(
                            self.home,
                            ticket_path,
                        )
                        self.assertIsNotNone(ticket)
                        assert ticket is not None
                    else:
                        ticket = self._publish_legacy_cleanup_ticket(version=version)
                    budget = MODULE.PendingCleanupActionBudget(
                        MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN
                    )
                    budget.consume_control_actions(budget.limit)
                    with (
                        mock.patch.object(
                            MODULE,
                            "_cleanup_ready_pending_batches",
                            return_value=0,
                        ) as cleanup,
                        mock.patch.object(
                            MODULE,
                            "_stage_release_tree_for_install",
                        ) as stage,
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            rf"ticket v{version}",
                        ),
                    ):
                        MODULE._install_release_set_unlocked(
                            case_home,
                            releases,
                            dry_run=False,
                            allow_cross_owner=False,
                            cleanup_budget=budget,
                        )

                    cleanup.assert_called_once_with(case_home, budget=budget)
                    stage.assert_not_called()
                    self.assertTrue(ticket.path.is_file())
                    self.assertEqual(case_target.read_bytes(), target_before)
                    self.assertEqual(case_state_path.read_bytes(), state_before)
                finally:
                    self.home = original_home

    def test_truncated_staging_marker_publish_temp_recovers_and_cleans_batch(
        self,
    ) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        for relative_path in (
            Path("pending/before"),
            Path("pending/stage"),
            Path("pending/evidence"),
            Path("pending/state"),
            Path("pending/cleanup"),
            Path("pending/claims/before"),
            Path("pending/claims/after"),
        ):
            directory_fd = MODULE._open_or_create_directory_beneath(
                self.home,
                batch_root / relative_path,
                mode=0o700,
            )
            MODULE._close_fd_quietly(directory_fd)
        marker_path = batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
        temp_path = marker_path.with_name(
            marker_path.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        )
        temp_path.write_bytes(b"{\n")
        temp_path.chmod(0o600)
        batch_identity = (batch_root.stat().st_dev, batch_root.stat().st_ino)
        expected_marker = MODULE._pending_staging_marker_payload(
            batch_root,
            batch_identity,
        )
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        real_publish = MODULE._publish_pending_batch_cleanup_ticket_for_root

        def publish_ticket(home: Path, root: Path, payload: bytes) -> None:
            self.assertEqual(root, batch_root)
            self.assertEqual(marker_path.read_bytes(), expected_marker)
            real_publish(home, root, payload)
            self.assertTrue(ticket_path.is_file())

        with mock.patch.object(
            MODULE,
            "_publish_pending_batch_cleanup_ticket_for_root",
            side_effect=publish_ticket,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertFalse(temp_path.exists())
        self.assertFalse(marker_path.exists())
        self.assertFalse(ticket_path.exists())
        self.assertFalse(batch_root.exists())

    def test_v6_ephemeral_cleanup_scanner_recovers_every_mutation_crash_window(
        self,
    ) -> None:
        for stage in (
            "ticket-temp",
            "canonical-alias",
            "alias-private",
            "private-unlink-before",
            "private-unlink-after",
        ):
            with self.subTest(stage=stage):
                case_home = self.root / f"ephemeral-crash-{stage}"
                install(self.first_release, case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                expected = MODULE._read_regular_file_snapshot_beneath(
                    case_home,
                    case_target,
                    require_managed_access=False,
                )
                for _index in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1):
                    MODULE._quarantine_batch_root(case_home, [])

                with (
                    self._receiptless_cleanup_crash_patch(stage),
                    self.assertRaisesRegex(SystemExit, "injected crash"),
                ):
                    MODULE._delete_exact_regular_publication_without_pending_receipt(
                        case_home,
                        case_target,
                        expected,
                    )

                index_root = MODULE._pending_cleanup_index_path(case_home)
                if stage == "ticket-temp":
                    temps = list(index_root.glob("*.json.tmp"))
                    self.assertEqual(len(temps), 1)
                    self.assertFalse(list(index_root.glob("*.json")))
                else:
                    ticket = self._only_cleanup_ticket_for_home(case_home)
                    self.assertEqual(ticket.version, 6)
                    self.assertEqual(ticket.kind, "ephemeral-quarantine-leaf")
                self.assertEqual(
                    MODULE._quarantine_batch_count(case_home),
                    MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1,
                )

                self.assertEqual(
                    MODULE._cleanup_ready_pending_batches(case_home),
                    1,
                )

                self.assertFalse(list(index_root.glob("*.json")))
                self.assertFalse(list(index_root.glob("*.json.tmp")))
                self.assertFalse(case_target.exists())
                quarantine_root = (
                    MODULE._personal_sync_root(case_home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                )
                self.assertFalse(
                    tuple(quarantine_root.glob(".codex-ephemeral-cleanup-*"))
                )
                self.assertEqual(
                    MODULE._quarantine_batch_count(case_home),
                    MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1,
                )
                self.assertIsInstance(
                    MODULE._quarantine_batch_root(case_home, []),
                    Path,
                )

    def test_v5_ticket_temp_before_rename_recovers_with_v8_allocation(self) -> None:
        case_home = self.root / "ephemeral-v5-temp-with-allocation"
        case_home.mkdir()
        allocation = MODULE._quarantine_batch_root(
            case_home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        self.assertIsInstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        batch_root = allocation.batch_root
        allocation_path = allocation.binding.allocation_ticket.path
        try:
            with (
                self._receiptless_cleanup_crash_patch("ticket-temp"),
                self.assertRaisesRegex(SystemExit, "before ticket temp rename"),
            ):
                allocation.create_leaf()
        finally:
            allocation.revoke_reclaim()
            allocation.close()

        ticket_path = MODULE._pending_cleanup_ticket_path(
            case_home,
            batch_root.name,
        )
        temp_path = ticket_path.with_name(
            batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        self.assertTrue(temp_path.is_file())
        self.assertFalse(ticket_path.exists())
        self.assertTrue(allocation_path.is_file())
        staged_payload = temp_path.read_bytes()
        staged_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
        staged = json.loads(staged_payload)
        self.assertEqual(staged["version"], 5)
        self.assertNotIn("size", staged["metadata"])

        self.assertTrue(
            MODULE._promote_pending_ephemeral_cleanup_ticket_temp(
                case_home,
                temp_path,
            )
        )
        self.assertFalse(temp_path.exists())
        self.assertEqual(ticket_path.read_bytes(), staged_payload)
        self.assertEqual(
            (ticket_path.stat().st_dev, ticket_path.stat().st_ino),
            staged_identity,
        )
        promoted = MODULE._read_pending_cleanup_ticket(case_home, ticket_path)
        self.assertIsNotNone(promoted)
        assert promoted is not None
        self.assertEqual(promoted.version, 5)
        self.assertIsNone(promoted.metadata_size)

        self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)

        self.assertFalse(temp_path.exists())
        self.assertFalse(ticket_path.exists())
        self.assertFalse(allocation_path.exists())
        self.assertFalse(batch_root.exists())

    def test_v5_ticket_temp_with_noncanonical_size_is_not_promoted(self) -> None:
        case_home = self.root / "ephemeral-v5-malformed-temp"
        install(self.first_release, case_home, SHA_A)
        ticket = self._make_v5_empty_ticket(case_home, case_home / ROLE_TARGET)
        malformed = json.loads(ticket.snapshot.payload or b"{}")
        malformed["metadata"]["size"] = (
            (ticket.batch_root / "metadata.json").stat().st_size
        )
        malformed_payload = MODULE._bounded_json_document(
            malformed,
            max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup ticket exceeds the size limit",
        )
        ticket.path.unlink()
        temp_path = ticket.path.with_name(
            ticket.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        temp_path.write_bytes(malformed_payload)
        temp_path.chmod(0o600)
        temp_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)

        self.assertFalse(
            MODULE._promote_pending_ephemeral_cleanup_ticket_temp(
                case_home,
                temp_path,
            )
        )
        self.assertFalse(ticket.path.exists())
        self.assertEqual(temp_path.read_bytes(), malformed_payload)
        self.assertEqual(
            (temp_path.stat().st_dev, temp_path.stat().st_ino),
            temp_identity,
        )

        self.assertEqual(
            MODULE._cleanup_pending_cleanup_ticket_temps(case_home),
            1,
        )
        self.assertFalse(temp_path.exists())
        self.assertFalse(ticket.path.exists())
        self.assertTrue(ticket.batch_root.is_dir())

    def test_v5_ticket_temp_revalidation_failure_is_retained(self) -> None:
        case_home = self.root / "ephemeral-v5-temp-revalidation-failure"
        install(self.first_release, case_home, SHA_A)
        ticket = self._make_v5_empty_ticket(case_home, case_home / ROLE_TARGET)
        temp_path = ticket.path.with_name(
            ticket.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        ticket.path.rename(temp_path)
        expected_payload = temp_path.read_bytes()
        expected_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
        real_read = MODULE._read_managed_state_file_snapshot

        def fail_bound_temp_reread(
            home: Path,
            path: Path,
            parent_fd: int,
            *,
            expected_identity=None,
            maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
        ):
            if path == temp_path and expected_identity is not None:
                raise OSError("injected bound temp reread failure")
            return real_read(
                home,
                path,
                parent_fd,
                expected_identity=expected_identity,
                maximum_bytes=maximum_bytes,
            )

        with (
            mock.patch.object(
                MODULE,
                "_read_managed_state_file_snapshot",
                side_effect=fail_bound_temp_reread,
            ),
            mock.patch.object(
                MODULE,
                "_discard_incomplete_pending_cleanup_ticket",
                side_effect=AssertionError("destructive cleanup was attempted"),
            ) as discard,
            self.assertRaisesRegex(OSError, "bound temp reread failure"),
        ):
            MODULE._cleanup_pending_cleanup_ticket_temps(case_home)

        discard.assert_not_called()
        self.assertFalse(ticket.path.exists())
        self.assertTrue(temp_path.is_file())
        self.assertEqual(temp_path.read_bytes(), expected_payload)
        self.assertEqual(
            (temp_path.stat().st_dev, temp_path.stat().st_ino),
            expected_identity,
        )
        self.assertTrue(ticket.batch_root.is_dir())

    def test_v5_ticket_temp_policy_failure_is_retained(self) -> None:
        case_home = self.root / "ephemeral-v5-temp-policy-failure"
        install(self.first_release, case_home, SHA_A)
        ticket = self._make_v5_empty_ticket(case_home, case_home / ROLE_TARGET)
        temp_path = ticket.path.with_name(
            ticket.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        ticket.path.rename(temp_path)
        temp_path.chmod(0o640)
        expected_payload = temp_path.read_bytes()
        expected_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)

        with (
            mock.patch.object(
                MODULE,
                "_discard_incomplete_pending_cleanup_ticket",
                side_effect=AssertionError("destructive cleanup was attempted"),
            ) as discard,
            self.assertRaisesRegex(
                MODULE._PendingCleanupAccessPolicyError,
                "mode 0640",
            ),
        ):
            MODULE._cleanup_pending_cleanup_ticket_temps(case_home)

        discard.assert_not_called()
        self.assertFalse(ticket.path.exists())
        self.assertTrue(temp_path.is_file())
        self.assertEqual(temp_path.read_bytes(), expected_payload)
        self.assertEqual(
            (temp_path.stat().st_dev, temp_path.stat().st_ino),
            expected_identity,
        )
        self.assertEqual(MODULE.stat.S_IMODE(temp_path.stat().st_mode), 0o640)
        self.assertTrue(ticket.batch_root.is_dir())

    def test_malformed_temp_classification_never_discards_valid_replacement(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-temp-classification-replacement"
        install(self.first_release, case_home, SHA_A)
        ticket = self._make_v5_empty_ticket(case_home, case_home / ROLE_TARGET)
        valid_payload = ticket.snapshot.payload
        self.assertIsNotNone(valid_payload)
        assert valid_payload is not None
        malformed = json.loads(valid_payload)
        malformed["metadata"]["size"] = (
            (ticket.batch_root / "metadata.json").stat().st_size
        )
        malformed_payload = MODULE._bounded_json_document(
            malformed,
            max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup ticket exceeds the size limit",
        )
        ticket.path.unlink()
        temp_path = ticket.path.with_name(
            ticket.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        temp_path.write_bytes(malformed_payload)
        temp_path.chmod(0o600)
        classified_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
        real_promote = MODULE._promote_pending_ephemeral_cleanup_ticket_temp
        replacement_identity: tuple[int, int] | None = None

        def replace_after_classification(
            home: Path,
            path: Path,
            *,
            classified_snapshot=None,
        ) -> bool:
            nonlocal replacement_identity
            promoted = real_promote(
                home,
                path,
                classified_snapshot=classified_snapshot,
            )
            self.assertFalse(promoted)
            replacement_path = path.with_name(path.name + ".replacement")
            replacement_path.write_bytes(valid_payload)
            replacement_path.chmod(0o600)
            replacement_metadata = replacement_path.stat()
            replacement_identity = (
                replacement_metadata.st_dev,
                replacement_metadata.st_ino,
            )
            self.assertNotEqual(replacement_identity, classified_identity)
            os.replace(replacement_path, path)
            return False

        with (
            mock.patch.object(
                MODULE,
                "_promote_pending_ephemeral_cleanup_ticket_temp",
                side_effect=replace_after_classification,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "changed before read"),
        ):
            MODULE._cleanup_pending_cleanup_ticket_temps(case_home)

        self.assertIsNotNone(replacement_identity)
        self.assertFalse(ticket.path.exists())
        self.assertTrue(temp_path.is_file())
        self.assertEqual(temp_path.read_bytes(), valid_payload)
        self.assertEqual(
            (temp_path.stat().st_dev, temp_path.stat().st_ino),
            replacement_identity,
        )
        self.assertTrue(ticket.batch_root.is_dir())

    def test_malformed_v8_classification_never_discards_valid_replacement(
        self,
    ) -> None:
        case_home = self.root / "allocation-temp-classification-replacement"
        case_home.mkdir()
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            case_home,
            quarantine_root,
            mode=0o700,
        )
        try:
            root_identity = MODULE._directory_identity(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        batch_name = "20260905T000000Z-3-1"
        valid_payload = MODULE._pending_quarantine_allocation_payload(
            batch_name,
            root_identity,
            b"valid replacement metadata\n",
        )
        malformed = json.loads(valid_payload)
        malformed["unexpected"] = True
        malformed_payload = MODULE._bounded_json_document(
            malformed,
            max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
            overflow_error="pending quarantine allocation exceeds the size limit",
        )
        index_fd = MODULE._open_or_create_directory_beneath(
            case_home,
            MODULE._pending_cleanup_index_path(case_home),
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        canonical_path = MODULE._pending_quarantine_allocation_path(
            case_home,
            batch_name,
        )
        temp_path = canonical_path.with_name(
            batch_name + MODULE.PENDING_QUARANTINE_ALLOCATION_TEMP_SUFFIX
        )
        temp_path.write_bytes(malformed_payload)
        temp_path.chmod(0o600)
        classified_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
        real_promote = MODULE._promote_pending_quarantine_allocation_temp
        replacement_identity: tuple[int, int] | None = None

        def replace_after_classification(
            home: Path,
            path: Path,
            *,
            classified_snapshot=None,
        ) -> bool:
            nonlocal replacement_identity
            promoted = real_promote(
                home,
                path,
                classified_snapshot=classified_snapshot,
            )
            self.assertFalse(promoted)
            replacement_path = path.with_name(path.name + ".replacement")
            replacement_path.write_bytes(valid_payload)
            replacement_path.chmod(0o600)
            replacement_metadata = replacement_path.stat()
            replacement_identity = (
                replacement_metadata.st_dev,
                replacement_metadata.st_ino,
            )
            self.assertNotEqual(replacement_identity, classified_identity)
            os.replace(replacement_path, path)
            return False

        with (
            mock.patch.object(
                MODULE,
                "_promote_pending_quarantine_allocation_temp",
                side_effect=replace_after_classification,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "changed before read"),
        ):
            MODULE._cleanup_pending_cleanup_ticket_temps(case_home)

        self.assertIsNotNone(replacement_identity)
        self.assertFalse(canonical_path.exists())
        self.assertTrue(temp_path.is_file())
        self.assertEqual(temp_path.read_bytes(), valid_payload)
        self.assertEqual(
            (temp_path.stat().st_dev, temp_path.stat().st_ino),
            replacement_identity,
        )
        self.assertFalse((quarantine_root / batch_name).exists())

    def test_duplicate_v5_v6_v7_temp_revalidates_canonical_at_delete_boundary(
        self,
    ) -> None:
        for version, mutation in ((5, "content"), (6, "identity"), (7, "policy")):
            with self.subTest(ticket_version=version, mutation=mutation):
                case_home = self.root / f"duplicate-v{version}-{mutation}"
                allocation = None
                if version == 5:
                    install(self.first_release, case_home, SHA_A)
                    ticket = self._make_v5_empty_ticket(
                        case_home,
                        case_home / ROLE_TARGET,
                    )
                elif version == 6:
                    install(self.first_release, case_home, SHA_A)
                    target = case_home / ROLE_TARGET
                    expected = MODULE._read_regular_file_snapshot_beneath(
                        case_home,
                        target,
                        require_managed_access=False,
                    )
                    ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
                        case_home,
                        target,
                        expected,
                    )
                else:
                    case_home.mkdir()
                    allocation = MODULE._quarantine_batch_root(
                        case_home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                    self.assertIsInstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    assert isinstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                        case_home,
                        allocation.binding,
                    )
                    allocation.revoke_reclaim()
                    allocation.close()

                temp_path = ticket.path.with_name(
                    ticket.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
                )
                temp_path.write_bytes(ticket.snapshot.payload or b"")
                temp_path.chmod(0o600)
                temp_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
                canonical_identity = (
                    ticket.path.stat().st_dev,
                    ticket.path.stat().st_ino,
                )
                canonical_payload = ticket.path.read_bytes()
                real_delete = MODULE._isolate_and_delete_pending_cleanup_file
                mutated_identity: tuple[int, int] | None = None
                mutated_payload: bytes | None = None

                def mutate_canonical_then_delete(
                    home: Path,
                    path: Path,
                    parent_fd: int,
                    expected_snapshot,
                    *,
                    label: str,
                    maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
                    mutation_revalidator=None,
                ) -> None:
                    nonlocal mutated_identity, mutated_payload
                    if path == temp_path and mutated_identity is None:
                        if mutation == "content":
                            replacement = (
                                bytes([canonical_payload[0] ^ 1])
                                + canonical_payload[1:]
                            )
                            with ticket.path.open("r+b") as canonical_file:
                                canonical_file.write(replacement)
                                canonical_file.truncate()
                                canonical_file.flush()
                                os.fsync(canonical_file.fileno())
                        elif mutation == "identity":
                            replacement_path = ticket.path.with_name(
                                ticket.path.name + ".replacement"
                            )
                            replacement_path.write_bytes(canonical_payload)
                            replacement_path.chmod(0o600)
                            os.replace(replacement_path, ticket.path)
                        else:
                            ticket.path.chmod(0o640)
                        canonical_metadata = ticket.path.stat()
                        mutated_identity = (
                            canonical_metadata.st_dev,
                            canonical_metadata.st_ino,
                        )
                        mutated_payload = ticket.path.read_bytes()
                    real_delete(
                        home,
                        path,
                        parent_fd,
                        expected_snapshot,
                        label=label,
                        maximum_bytes=maximum_bytes,
                        mutation_revalidator=mutation_revalidator,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                        side_effect=mutate_canonical_then_delete,
                    ),
                    self.assertRaises(MODULE.SyncError),
                ):
                    MODULE._promote_pending_ephemeral_cleanup_ticket_temp(
                        case_home,
                        temp_path,
                    )

                self.assertIsNotNone(mutated_identity)
                self.assertTrue(temp_path.is_file())
                self.assertEqual(
                    (temp_path.stat().st_dev, temp_path.stat().st_ino),
                    temp_identity,
                )
                self.assertEqual(temp_path.read_bytes(), ticket.snapshot.payload)
                self.assertTrue(ticket.path.is_file())
                self.assertEqual(ticket.path.read_bytes(), mutated_payload)
                if mutation == "identity":
                    self.assertNotEqual(mutated_identity, canonical_identity)
                else:
                    self.assertEqual(mutated_identity, canonical_identity)
                if mutation == "policy":
                    self.assertEqual(
                        MODULE.stat.S_IMODE(ticket.path.stat().st_mode),
                        0o640,
                    )

    def test_duplicate_v8_temp_retained_when_canonical_disappears(self) -> None:
        case_home = self.root / "duplicate-v8-disappearance"
        case_home.mkdir()
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            case_home,
            quarantine_root,
            mode=0o700,
        )
        try:
            ticket = MODULE._publish_pending_quarantine_allocation_ticket(
                case_home,
                quarantine_root,
                quarantine_fd,
                "20260905T000000Z-2-1",
                b"duplicate allocation fixture\n",
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        temp_path = ticket.path.with_name(
            ticket.batch_root.name + MODULE.PENDING_QUARANTINE_ALLOCATION_TEMP_SUFFIX
        )
        temp_path.write_bytes(ticket.snapshot.payload or b"")
        temp_path.chmod(0o600)
        temp_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file
        disappeared = False

        def remove_canonical_then_delete(
            home: Path,
            path: Path,
            parent_fd: int,
            expected_snapshot,
            *,
            label: str,
            maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
            mutation_revalidator=None,
        ) -> None:
            nonlocal disappeared
            if path == temp_path and not disappeared:
                ticket.path.unlink()
                disappeared = True
            real_delete(
                home,
                path,
                parent_fd,
                expected_snapshot,
                label=label,
                maximum_bytes=maximum_bytes,
                mutation_revalidator=mutation_revalidator,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=remove_canonical_then_delete,
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE._promote_pending_quarantine_allocation_temp(
                case_home,
                temp_path,
            )

        self.assertTrue(disappeared)
        self.assertFalse(ticket.path.exists())
        self.assertTrue(temp_path.is_file())
        self.assertEqual(temp_path.read_bytes(), ticket.snapshot.payload)
        self.assertEqual(
            (temp_path.stat().st_dev, temp_path.stat().st_ino),
            temp_identity,
        )

    def test_duplicate_temp_tombstone_revalidates_canonical_before_unlink(
        self,
    ) -> None:
        case_home = self.root / "duplicate-v5-second-boundary"
        install(self.first_release, case_home, SHA_A)
        ticket = self._make_v5_empty_ticket(case_home, case_home / ROLE_TARGET)
        canonical_payload = ticket.path.read_bytes()
        canonical_identity = (
            ticket.path.stat().st_dev,
            ticket.path.stat().st_ino,
        )
        temp_path = ticket.path.with_name(
            ticket.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        temp_path.write_bytes(canonical_payload)
        temp_path.chmod(0o600)
        temp_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
        real_require = MODULE._require_pending_cleanup_file_snapshot_unchanged
        boundary_checks = 0
        replacement_identity: tuple[int, int] | None = None

        def replace_canonical_at_second_boundary(
            home: Path,
            path: Path,
            parent_fd: int,
            expected_snapshot,
            *,
            label: str,
            maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
        ):
            nonlocal boundary_checks, replacement_identity
            if path == ticket.path:
                boundary_checks += 1
                if boundary_checks == 2:
                    replacement_path = path.with_name(path.name + ".replacement")
                    replacement_path.write_bytes(canonical_payload)
                    replacement_path.chmod(0o600)
                    replacement_metadata = replacement_path.stat()
                    replacement_identity = (
                        replacement_metadata.st_dev,
                        replacement_metadata.st_ino,
                    )
                    self.assertNotEqual(replacement_identity, canonical_identity)
                    os.replace(replacement_path, path)
            return real_require(
                home,
                path,
                parent_fd,
                expected_snapshot,
                label=label,
                maximum_bytes=maximum_bytes,
            )

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_file_snapshot_unchanged",
                side_effect=replace_canonical_at_second_boundary,
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE._promote_pending_ephemeral_cleanup_ticket_temp(
                case_home,
                temp_path,
            )

        self.assertEqual(boundary_checks, 2)
        self.assertIsNotNone(replacement_identity)
        self.assertFalse(temp_path.exists())
        retained = tuple(
            temp_path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{temp_path.name}-*"
            )
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), canonical_payload)
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            temp_identity,
        )
        self.assertEqual(ticket.path.read_bytes(), canonical_payload)
        self.assertEqual(
            (ticket.path.stat().st_dev, ticket.path.stat().st_ino),
            replacement_identity,
        )

    def test_duplicate_canonical_replacement_between_capture_and_parser_is_retained(
        self,
    ) -> None:
        case_home = self.root / "duplicate-v6-capture-parser-replacement"
        install(self.first_release, case_home, SHA_A)
        target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            target,
            expected,
        )
        canonical_payload = ticket.path.read_bytes()
        canonical_identity = (
            ticket.path.stat().st_dev,
            ticket.path.stat().st_ino,
        )
        temp_path = ticket.path.with_name(
            ticket.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        temp_path.write_bytes(canonical_payload)
        temp_path.chmod(0o600)
        temp_identity = (temp_path.stat().st_dev, temp_path.stat().st_ino)
        real_read_ticket = MODULE._read_pending_cleanup_ticket
        replacement_identity: tuple[int, int] | None = None

        def replace_before_bound_parser(
            home: Path,
            path: Path,
            *,
            expected_ticket_identity=None,
            allow_temporary: bool = False,
            _captured_snapshot=None,
        ):
            nonlocal replacement_identity
            if (
                path == ticket.path
                and expected_ticket_identity is not None
                and replacement_identity is None
            ):
                replacement_path = path.with_name(path.name + ".replacement")
                replacement_path.write_bytes(canonical_payload)
                replacement_path.chmod(0o600)
                replacement_metadata = replacement_path.stat()
                replacement_identity = (
                    replacement_metadata.st_dev,
                    replacement_metadata.st_ino,
                )
                self.assertNotEqual(replacement_identity, canonical_identity)
                os.replace(replacement_path, path)
            return real_read_ticket(
                home,
                path,
                expected_ticket_identity=expected_ticket_identity,
                allow_temporary=allow_temporary,
                _captured_snapshot=_captured_snapshot,
            )

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                side_effect=replace_before_bound_parser,
            ),
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=AssertionError("temp deletion was attempted"),
            ) as delete,
            self.assertRaisesRegex(MODULE.SyncError, "changed before read"),
        ):
            MODULE._promote_pending_ephemeral_cleanup_ticket_temp(
                case_home,
                temp_path,
            )

        delete.assert_not_called()
        self.assertIsNotNone(replacement_identity)
        self.assertTrue(temp_path.is_file())
        self.assertEqual(temp_path.read_bytes(), canonical_payload)
        self.assertEqual(
            (temp_path.stat().st_dev, temp_path.stat().st_ino),
            temp_identity,
        )
        self.assertTrue(ticket.path.is_file())
        self.assertEqual(ticket.path.read_bytes(), canonical_payload)
        self.assertEqual(
            (ticket.path.stat().st_dev, ticket.path.stat().st_ino),
            replacement_identity,
        )

    def test_v5_legacy_ticket_without_metadata_size_recovers(self) -> None:
        case_home = self.root / "ephemeral-v5-no-metadata-size"
        install(self.first_release, case_home, SHA_A)
        ticket = self._make_v5_empty_ticket(case_home, case_home / ROLE_TARGET)
        payload = json.loads(ticket.snapshot.payload or b"{}")

        self.assertEqual(payload["version"], 5)
        self.assertNotIn("size", payload["metadata"])
        self.assertIsNone(ticket.metadata_size)
        self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())

    def test_v5_ephemeral_ticket_never_authorizes_a_retained_payload(self) -> None:
        case_home = self.root / "ephemeral-retained-payload"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        parent_fd = MODULE._open_directory_beneath(case_home, case_target.parent)
        try:
            result = MODULE._move_regular_leaf_to_unique_quarantine(
                case_home,
                case_target.parent,
                parent_fd,
                case_target.name,
                label="legacy-v5-payload",
                expected=expected,
                retain_batch_binding=True,
            )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        _quarantine_path, _moved, binding = result
        # Exercise historical v5 recovery semantics independently from the v8
        # fence that current private moves retire before returning.
        binding = replace(binding, allocation_ticket=None)
        ticket = MODULE._publish_pending_ephemeral_quarantine_cleanup_ticket(
            case_home,
            binding,
        )
        self.assertEqual(ticket.version, 5)
        payloads = tuple((ticket.batch_root / "leaf").iterdir())
        self.assertEqual(len(payloads), 1)
        payload_identity = (
            payloads[0].stat().st_dev,
            payloads[0].stat().st_ino,
        )
        payload = payloads[0].read_bytes()

        with self.assertRaisesRegex(MODULE.SyncError, "leaf is not empty"):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(ticket.path.is_file())
        self.assertEqual(payloads[0].read_bytes(), payload)
        self.assertEqual(
            (payloads[0].stat().st_dev, payloads[0].stat().st_ino),
            payload_identity,
        )
        with self.assertRaisesRegex(MODULE.SyncError, "ticket v5"):
            MODULE._require_no_pending_terminal_mutation_authority(case_home)

    def test_v6_ephemeral_cleanup_preserves_foreign_payload_replacements(
        self,
    ) -> None:
        for case in (
            "private-replacement",
            "canonical-reappearance",
            "alias-reappearance",
        ):
            with self.subTest(case=case):
                case_home = self.root / f"ephemeral-v6-{case}"
                install(self.first_release, case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                expected = MODULE._read_regular_file_snapshot_beneath(
                    case_home,
                    case_target,
                    require_managed_access=False,
                )
                with (
                    self._receiptless_cleanup_crash_patch("alias-private"),
                    self.assertRaisesRegex(SystemExit, "after private isolation"),
                ):
                    MODULE._delete_exact_regular_publication_without_pending_receipt(
                        case_home,
                        case_target,
                        expected,
                    )
                ticket = self._only_cleanup_ticket_for_home(case_home)
                private_path = (
                    MODULE._personal_sync_root(case_home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                    / MODULE._pending_ephemeral_quarantine_leaf_name(
                        ticket.batch_root.name
                    )
                )
                if case == "private-replacement":
                    private_path.unlink()
                    protected = private_path
                elif case == "alias-reappearance":
                    protected = case_target.with_name(
                        MODULE._pending_ephemeral_public_alias_name(
                            ticket.batch_root.name
                        )
                    )
                else:
                    protected = case_target
                protected.write_bytes(b"foreign replacement\n")
                protected.chmod(0o600)
                protected_identity = (
                    protected.stat().st_dev,
                    protected.stat().st_ino,
                )

                expected_error = (
                    "retained replacement as isolated evidence"
                    if case == "private-replacement"
                    else "reappeared after private isolation"
                )
                with self.assertRaisesRegex(MODULE.SyncError, expected_error):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertTrue(ticket.path.is_file())
                evidence = tuple(
                    (
                        MODULE._personal_sync_root(case_home)
                        / MODULE.QUARANTINE_RELATIVE_PATH
                    ).glob(".codex-ephemeral-cleanup-*")
                )
                if case == "private-replacement":
                    self.assertFalse(case_target.exists())
                    protected_evidence = tuple(
                        path
                        for path in evidence
                        if (path.stat().st_dev, path.stat().st_ino)
                        == protected_identity
                    )
                    self.assertEqual(len(protected_evidence), 1)
                    self.assertEqual(
                        protected_evidence[0].read_bytes(),
                        b"foreign replacement\n",
                    )
                else:
                    self.assertEqual(protected.read_bytes(), b"foreign replacement\n")
                    self.assertEqual(
                        (protected.stat().st_dev, protected.stat().st_ino),
                        protected_identity,
                    )
                    self.assertEqual(len(evidence), 1)

    def test_v6_private_inode_reuse_before_receipt_is_foreign_evidence(self) -> None:
        case_home = self.root / "ephemeral-v6-private-inode-reuse-before-receipt"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        exact_alias = case_target.with_name(
            MODULE._pending_ephemeral_public_alias_name(ticket.batch_root.name)
        )
        case_target.rename(exact_alias)
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        foreign_private = quarantine_root / (
            MODULE._pending_ephemeral_quarantine_leaf_name(ticket.batch_root.name)
        )
        foreign_payload = b"foreign payload with recycled inode identity\n"
        foreign_private.write_bytes(foreign_payload)
        foreign_private.chmod(0o600)
        foreign_metadata = foreign_private.stat()
        foreign_identity = (foreign_metadata.st_dev, foreign_metadata.st_ino)
        self.assertNotEqual(
            hashlib.sha256(foreign_payload).hexdigest(),
            expected.sha256,
        )

        with (
            self._recycled_private_inode_identity(
                foreign_private,
                expected,
            ) as snapshot_names,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained replacement as isolated evidence",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertIn(foreign_private.name, snapshot_names)
        self.assertTrue(ticket.path.is_file())
        self.assertIsNone(
            MODULE._read_pending_cleanup_terminal_validation(
                case_home,
                ticket,
                ticket.quarantine_root_identity,
            )
        )
        self.assertEqual(foreign_private.read_bytes(), foreign_payload)
        self.assertEqual(
            (foreign_private.stat().st_dev, foreign_private.stat().st_ino),
            foreign_identity,
        )
        retained_foreign = quarantine_root / "preserved-inode-reuse-evidence"
        foreign_private.rename(retained_foreign)

        self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(os.path.lexists(exact_alias))
        self.assertEqual(retained_foreign.read_bytes(), foreign_payload)
        self.assertEqual(
            (retained_foreign.stat().st_dev, retained_foreign.stat().st_ino),
            foreign_identity,
        )

    def test_v6_private_inode_reuse_after_receipt_is_foreign_evidence(self) -> None:
        case_home = self.root / "ephemeral-v6-private-inode-reuse-after-receipt"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            self._receiptless_cleanup_crash_patch("private-unlink-before"),
            self.assertRaisesRegex(SystemExit, "before private unlink"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        ticket = self._only_cleanup_ticket_for_home(case_home)
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        private_entries = tuple(
            quarantine_root.glob(".codex-ephemeral-cleanup-*.delete-*")
        )
        self.assertEqual(len(private_entries), 1)
        foreign_private = private_entries[0]
        foreign_private.unlink()
        foreign_payload = b"foreign tombstone with recycled inode identity\n"
        foreign_private.write_bytes(foreign_payload)
        foreign_private.chmod(0o600)
        foreign_metadata = foreign_private.stat()
        foreign_identity = (foreign_metadata.st_dev, foreign_metadata.st_ino)
        self.assertNotEqual(
            hashlib.sha256(foreign_payload).hexdigest(),
            expected.sha256,
        )

        with (
            self._recycled_private_inode_identity(
                foreign_private,
                expected,
            ) as snapshot_names,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained replacement as isolated evidence",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertIn(foreign_private.name, snapshot_names)
        self.assertTrue(ticket.path.is_file())
        self.assertIsNotNone(
            MODULE._read_pending_cleanup_terminal_validation(
                case_home,
                ticket,
                ticket.quarantine_root_identity,
            )
        )
        self.assertEqual(foreign_private.read_bytes(), foreign_payload)
        self.assertEqual(
            (foreign_private.stat().st_dev, foreign_private.stat().st_ino),
            foreign_identity,
        )
        retained_foreign = quarantine_root / "preserved-tombstone-reuse-evidence"
        foreign_private.rename(retained_foreign)

        self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                case_home,
                ticket.batch_root.name,
            ).exists()
        )
        self.assertEqual(retained_foreign.read_bytes(), foreign_payload)
        self.assertEqual(
            (retained_foreign.stat().st_dev, retained_foreign.stat().st_ino),
            foreign_identity,
        )

    def test_v6_extra_private_identity_mismatch_skips_snapshot_and_authority(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-extra-private-identity-mismatch"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            self._receiptless_cleanup_crash_patch("alias-private"),
            self.assertRaisesRegex(SystemExit, "after private isolation"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        ticket = self._only_cleanup_ticket_for_home(case_home)
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        foreign_private = quarantine_root / (
            MODULE._pending_ephemeral_quarantine_leaf_name(ticket.batch_root.name)
            + "-retained-extra"
        )
        foreign_private.write_bytes(b"foreign extra private evidence\n")
        foreign_private.chmod(0o600)
        real_inventory = MODULE._pending_ephemeral_quarantine_private_inventory

        def report_extra_foreign_identity(
            quarantine_fd: int,
            batch_name: str,
        ) -> tuple[tuple[str, tuple[int, int]], ...]:
            return tuple(
                (
                    name,
                    (identity[0], identity[1] + 1)
                    if name == foreign_private.name
                    else identity,
                )
                for name, identity in real_inventory(quarantine_fd, batch_name)
            )

        with (
            mock.patch.object(
                MODULE,
                "_pending_ephemeral_quarantine_private_inventory",
                side_effect=report_extra_foreign_identity,
            ),
            mock.patch.object(
                MODULE,
                "_regular_file_snapshot_at",
                side_effect=AssertionError("identity prefilter should skip snapshots"),
            ),
            mock.patch.object(
                MODULE,
                "_publish_pending_cleanup_terminal_validation",
                wraps=MODULE._publish_pending_cleanup_terminal_validation,
            ) as publish_receipt,
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_ticket",
                wraps=MODULE._delete_pending_cleanup_ticket,
            ) as delete_ticket,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained replacement as isolated evidence while attempting "
                "private isolation",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        publish_receipt.assert_not_called()
        delete_ticket.assert_not_called()
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(foreign_private.is_file())

    def test_v6_private_snapshot_verification_failure_is_not_foreign_evidence(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-private-snapshot-verification"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            self._receiptless_cleanup_crash_patch("private-unlink-before"),
            self.assertRaisesRegex(SystemExit, "before private unlink"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        ticket = self._only_cleanup_ticket_for_home(case_home)
        for verification_error in (
            OSError("injected unreadable private payload"),
            MODULE.SyncError("injected private payload revalidation failure"),
        ):
            with self.subTest(error=type(verification_error).__name__):
                with (
                    mock.patch.object(
                        MODULE,
                        "_regular_file_snapshot_at",
                        side_effect=verification_error,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_delete_pending_cleanup_ticket",
                        wraps=MODULE._delete_pending_cleanup_ticket,
                    ) as delete_ticket,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "private snapshot verification failed",
                    ) as raised,
                ):
                    MODULE._cleanup_ready_pending_batches(case_home)

                causes: list[BaseException] = []
                cause: BaseException | None = raised.exception
                while cause is not None:
                    causes.append(cause)
                    cause = cause.__cause__
                self.assertIn(verification_error, causes)
                self.assertNotIn("retained replacement", str(raised.exception))
                delete_ticket.assert_not_called()
                self.assertTrue(ticket.path.is_file())

    def test_v6_ephemeral_cleanup_with_competing_alias_retires_canonical_first(
        self,
    ) -> None:
        for competing_index in (0, 1):
            with self.subTest(competing_index=competing_index):
                case_home = self.root / (
                    f"ephemeral-v6-competing-alias-{competing_index}"
                )
                install(self.first_release, case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                expected = MODULE._read_regular_file_snapshot_beneath(
                    case_home,
                    case_target,
                    require_managed_access=False,
                )
                ticket = (
                    MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
                        case_home,
                        case_target,
                        expected,
                    )
                )
                alias_names = MODULE._pending_ephemeral_public_alias_names(
                    ticket.batch_root.name
                )
                competing_alias = case_target.with_name(alias_names[competing_index])
                competing_alias.write_bytes(b"same-uid competing alias\n")
                competing_alias.chmod(0o600)
                competing_identity = (
                    competing_alias.stat().st_dev,
                    competing_alias.stat().st_ino,
                )
                self.assertEqual(competing_alias.stat().st_uid, os.geteuid())

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "retained replacement as isolated evidence",
                ):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertTrue(ticket.path.is_file())
                self.assertFalse(os.path.lexists(case_target))
                exact_aliases = tuple(
                    case_target.with_name(name)
                    for name in alias_names
                    if (
                        case_target.with_name(name).exists()
                        and (
                            case_target.with_name(name).stat().st_dev,
                            case_target.with_name(name).stat().st_ino,
                        )
                        == expected.file_identity
                    )
                )
                self.assertEqual(len(exact_aliases), 1)
                self.assertEqual(
                    hashlib.sha256(exact_aliases[0].read_bytes()).hexdigest(),
                    expected.sha256,
                )
                self.assertFalse(competing_alias.exists())
                evidence = (
                    MODULE._personal_sync_root(case_home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                    / MODULE._pending_ephemeral_quarantine_leaf_name(
                        ticket.batch_root.name
                    )
                )
                self.assertTrue(evidence.is_file())
                self.assertEqual(
                    (evidence.stat().st_dev, evidence.stat().st_ino),
                    competing_identity,
                )
                self.assertEqual(
                    evidence.read_bytes(),
                    b"same-uid competing alias\n",
                )

                exact_alias_identity = (
                    exact_aliases[0].stat().st_dev,
                    exact_aliases[0].stat().st_ino,
                )
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "retained replacement as isolated evidence",
                ):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertFalse(os.path.lexists(case_target))
                self.assertEqual(
                    (
                        exact_aliases[0].stat().st_dev,
                        exact_aliases[0].stat().st_ino,
                    ),
                    exact_alias_identity,
                )
                self.assertEqual(
                    (evidence.stat().st_dev, evidence.stat().st_ino),
                    competing_identity,
                )

    def test_v6_ephemeral_cleanup_with_full_alias_set_evacuates_exact_canonical(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-full-alias-set"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        alias_names = MODULE._pending_ephemeral_public_alias_names(
            ticket.batch_root.name
        )
        protected_aliases: dict[str, tuple[tuple[int, int], bytes]] = {}
        for index, alias_name in enumerate(alias_names):
            alias = case_target.with_name(alias_name)
            payload = f"foreign alias {index}\n".encode("utf-8")
            alias.write_bytes(payload)
            alias.chmod(0o600)
            metadata = alias.stat()
            protected_aliases[alias_name] = (
                (metadata.st_dev, metadata.st_ino),
                payload,
            )

        evidence_names = MODULE._pending_ephemeral_quarantine_evidence_names(
            ticket.batch_root.name
        )
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        protected_private: dict[str, tuple[tuple[int, int], bytes]] = {}
        for index, evidence_name in enumerate(evidence_names):
            evidence = quarantine_root / evidence_name
            payload = f"foreign private evidence {index}\n".encode("utf-8")
            evidence.write_bytes(payload)
            evidence.chmod(0o600)
            metadata = evidence.stat()
            protected_private[evidence_name] = (
                (metadata.st_dev, metadata.st_ino),
                payload,
            )
        real_rename = MODULE._rename_noreplace_at
        crashed = False

        def crash_after_direct_private_evacuation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal crashed
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                source_name == case_target.name
                and MODULE._pending_ephemeral_quarantine_final_private_base(
                    ticket.batch_root.name,
                    destination_name,
                )
                is not None
            ):
                crashed = True
                raise SystemExit("injected crash after direct private evacuation")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_direct_private_evacuation,
            ),
            self.assertRaisesRegex(SystemExit, "direct private evacuation"),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(crashed)
        self.assertFalse(os.path.lexists(case_target))
        quarantine_fd = MODULE._open_directory_beneath(case_home, quarantine_root)
        try:
            private = MODULE._pending_ephemeral_quarantine_private_inventory(
                quarantine_fd,
                ticket.batch_root.name,
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        exact_private = tuple(
            item for item in private if item[1] == expected.file_identity
        )
        self.assertEqual(len(exact_private), 1)
        self.assertIsNotNone(
            MODULE._pending_ephemeral_quarantine_final_private_base(
                ticket.batch_root.name,
                exact_private[0][0],
            )
        )
        self.assertIsNone(
            MODULE._read_pending_cleanup_terminal_validation(
                case_home,
                ticket,
                ticket.quarantine_root_identity,
            )
        )
        for alias_name, (identity, payload) in protected_aliases.items():
            alias = case_target.with_name(alias_name)
            self.assertEqual(alias.read_bytes(), payload)
            self.assertEqual(
                (alias.stat().st_dev, alias.stat().st_ino),
                identity,
            )
        for evidence_name, (identity, payload) in protected_private.items():
            evidence = quarantine_root / evidence_name
            self.assertEqual(evidence.read_bytes(), payload)
            self.assertEqual(
                (evidence.stat().st_dev, evidence.stat().st_ino),
                identity,
            )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "retained replacement as isolated evidence while attempting private isolation",
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(ticket.path.is_file())
        self.assertIsNone(
            MODULE._read_pending_cleanup_terminal_validation(
                case_home,
                ticket,
                ticket.quarantine_root_identity,
            )
        )
        self.assertFalse(os.path.lexists(case_target))
        for alias_name in protected_aliases:
            alias = case_target.with_name(alias_name)
            self.assertFalse(os.path.lexists(alias))
        quarantine_fd = MODULE._open_directory_beneath(case_home, quarantine_root)
        try:
            retained_private = MODULE._pending_ephemeral_quarantine_private_inventory(
                quarantine_fd,
                ticket.batch_root.name,
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        retained_by_identity = {identity: name for name, identity in retained_private}
        for identity, payload in protected_aliases.values():
            retained = quarantine_root / retained_by_identity[identity]
            self.assertEqual(retained.read_bytes(), payload)
        for evidence_name, (identity, payload) in protected_private.items():
            evidence = quarantine_root / evidence_name
            self.assertEqual(evidence.read_bytes(), payload)
            self.assertEqual(
                (evidence.stat().st_dev, evidence.stat().st_ino),
                identity,
            )
        exact_private = tuple(
            item for item in retained_private if item[1] == expected.file_identity
        )
        self.assertEqual(len(exact_private), 1)
        self.assertEqual(
            hashlib.sha256(
                (quarantine_root / exact_private[0][0]).read_bytes()
            ).hexdigest(),
            expected.sha256,
        )

    def test_v6_ephemeral_cleanup_retries_private_evacuation_name_collisions(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-private-name-collision"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        for index, alias_name in enumerate(
            MODULE._pending_ephemeral_public_alias_names(ticket.batch_root.name)
        ):
            alias = case_target.with_name(alias_name)
            alias.write_bytes(f"foreign alias {index}\n".encode("utf-8"))
            alias.chmod(0o600)
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        for index, evidence_name in enumerate(
            MODULE._pending_ephemeral_quarantine_evidence_names(ticket.batch_root.name)
        ):
            evidence = quarantine_root / evidence_name
            evidence.write_bytes(f"foreign evidence {index}\n".encode("utf-8"))
            evidence.chmod(0o600)

        real_rename = MODULE._rename_noreplace_at
        collided = False
        crashed = False
        collision_path: Path | None = None

        def collide_then_crash_after_retry(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal collided, crashed, collision_path
            direct_private_name = (
                source_name == case_target.name
                and MODULE._pending_ephemeral_quarantine_final_private_base(
                    ticket.batch_root.name,
                    destination_name,
                )
                is not None
            )
            if direct_private_name and not collided:
                collision_path = quarantine_root / destination_name
                descriptor = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_parent_fd,
                )
                try:
                    os.write(descriptor, b"racing foreign evidence\n")
                finally:
                    os.close(descriptor)
                collided = True
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if direct_private_name and collided:
                crashed = True
                raise SystemExit("injected crash after private retry")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=collide_then_crash_after_retry,
            ),
            self.assertRaisesRegex(SystemExit, "private retry"),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(collided)
        self.assertTrue(crashed)
        self.assertIsNotNone(collision_path)
        assert collision_path is not None
        self.assertEqual(collision_path.read_bytes(), b"racing foreign evidence\n")
        self.assertFalse(os.path.lexists(case_target))
        quarantine_fd = MODULE._open_directory_beneath(case_home, quarantine_root)
        try:
            private = MODULE._pending_ephemeral_quarantine_private_inventory(
                quarantine_fd,
                ticket.batch_root.name,
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        self.assertEqual(
            len([item for item in private if item[1] == expected.file_identity]),
            1,
        )

    def test_v6_ephemeral_cleanup_does_not_receipt_private_hardlink_while_public(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-private-hardlink-public"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        private_name = MODULE._pending_ephemeral_quarantine_evidence_names(
            ticket.batch_root.name
        )[0]
        os.link(case_target, quarantine_root / private_name)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_cleanup_terminal_validation",
                side_effect=AssertionError("must not receipt a public payload"),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "canonical payload changed before evacuation",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(case_target.is_file())
        self.assertTrue(ticket.path.is_file())
        self.assertIsNone(
            MODULE._read_pending_cleanup_terminal_validation(
                case_home,
                ticket,
                ticket.quarantine_root_identity,
            )
        )

    def test_v6_ephemeral_cleanup_rechecks_bound_authority_after_snapshot(
        self,
    ) -> None:
        for mutation in ("ticket", "parent"):
            with self.subTest(mutation=mutation):
                case_home = self.root / f"ephemeral-v6-final-boundary-{mutation}"
                install(self.first_release, case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                expected = MODULE._read_regular_file_snapshot_beneath(
                    case_home,
                    case_target,
                    require_managed_access=False,
                )
                ticket = (
                    MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
                        case_home,
                        case_target,
                        expected,
                    )
                )
                real_snapshot = MODULE._regular_file_snapshot_at
                target_snapshots = 0
                original_parent_mode = stat.S_IMODE(case_target.parent.stat().st_mode)

                def mutate_after_final_snapshot(
                    parent_fd: int,
                    name: str,
                    path: Path,
                    *,
                    maximum_bytes: int = MODULE.MAX_ARCHIVE_MEMBER_BYTES,
                ):
                    nonlocal target_snapshots
                    snapshot = real_snapshot(
                        parent_fd,
                        name,
                        path,
                        maximum_bytes=maximum_bytes,
                    )
                    if path != case_target:
                        return snapshot
                    target_snapshots += 1
                    if target_snapshots == 2:
                        if mutation == "ticket":
                            ticket.path.write_bytes(b"{}\n")
                        else:
                            case_target.parent.chmod(original_parent_mode | 0o022)
                    return snapshot

                try:
                    with (
                        mock.patch.object(
                            MODULE,
                            "_regular_file_snapshot_at",
                            side_effect=mutate_after_final_snapshot,
                        ),
                        mock.patch.object(MODULE, "_rename_noreplace_at") as rename,
                        self.assertRaises(MODULE.SyncError),
                    ):
                        MODULE._cleanup_ready_pending_batches(case_home)
                finally:
                    case_target.parent.chmod(original_parent_mode)

                self.assertEqual(target_snapshots, 2)
                rename.assert_not_called()
                self.assertTrue(case_target.is_file())
                self.assertTrue(ticket.path.is_file())

    def test_v6_ephemeral_cleanup_rejects_post_rename_destination_replacement(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-post-rename-replacement"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        for index, alias_name in enumerate(
            MODULE._pending_ephemeral_public_alias_names(ticket.batch_root.name)
        ):
            alias = case_target.with_name(alias_name)
            alias.write_bytes(f"foreign alias {index}\n".encode("utf-8"))
            alias.chmod(0o600)

        real_rename = MODULE._rename_noreplace_at
        replaced = False
        retained: Path | None = None

        def replace_destination_after_rename(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal replaced, retained
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                source_name != case_target.name
                or MODULE._pending_ephemeral_quarantine_final_private_base(
                    ticket.batch_root.name,
                    destination_name,
                )
                is None
            ):
                return
            os.unlink(destination_name, dir_fd=destination_parent_fd)
            descriptor = os.open(
                destination_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_parent_fd,
            )
            try:
                os.write(descriptor, b"foreign post-rename replacement\n")
            finally:
                os.close(descriptor)
            retained = (
                MODULE._personal_sync_root(case_home)
                / MODULE.QUARANTINE_RELATIVE_PATH
                / destination_name
            )
            replaced = True

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=replace_destination_after_rename,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "canonical replacement was retained as isolated evidence",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(replaced)
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(retained.read_bytes(), b"foreign post-rename replacement\n")
        self.assertFalse(os.path.lexists(case_target))
        self.assertTrue(ticket.path.is_file())

    def test_v6_ephemeral_cleanup_allows_benign_parent_child_churn(self) -> None:
        case_home = self.root / "ephemeral-v6-benign-parent-churn"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        real_snapshot = MODULE._regular_file_snapshot_at
        target_snapshots = 0
        churned = False

        def churn_after_final_snapshot(
            parent_fd: int,
            name: str,
            path: Path,
            *,
            maximum_bytes: int = MODULE.MAX_ARCHIVE_MEMBER_BYTES,
        ):
            nonlocal churned, target_snapshots
            snapshot = real_snapshot(
                parent_fd,
                name,
                path,
                maximum_bytes=maximum_bytes,
            )
            if path == case_target:
                target_snapshots += 1
                if target_snapshots == 2:
                    sibling = case_target.with_name(".unrelated-child-churn")
                    sibling.write_bytes(b"benign child churn\n")
                    sibling.chmod(0o600)
                    sibling.unlink()
                    churned = True
            return snapshot

        with mock.patch.object(
            MODULE,
            "_regular_file_snapshot_at",
            side_effect=churn_after_final_snapshot,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)

        self.assertTrue(churned)
        self.assertFalse(os.path.lexists(case_target))
        self.assertFalse(ticket.path.exists())

    def test_v6_canonical_evacuation_rejects_same_inode_metadata_changes(
        self,
    ) -> None:
        for mutation in ("content", "mode", "uid", "link-count"):
            with self.subTest(mutation=mutation):
                case_home = self.root / f"ephemeral-v6-canonical-{mutation}"
                install(self.first_release, case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                expected = MODULE._read_regular_file_snapshot_beneath(
                    case_home,
                    case_target,
                    require_managed_access=False,
                )
                ticket = (
                    MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
                        case_home,
                        case_target,
                        expected,
                    )
                )
                original_payload = case_target.read_bytes()
                real_snapshot = MODULE._regular_file_snapshot_at
                mutated = False

                def mutate_after_descriptor_capture(
                    parent_fd: int,
                    name: str,
                    path: Path,
                    *,
                    maximum_bytes: int = MODULE.MAX_ARCHIVE_MEMBER_BYTES,
                ):
                    nonlocal mutated
                    snapshot = real_snapshot(
                        parent_fd,
                        name,
                        path,
                        maximum_bytes=maximum_bytes,
                    )
                    if path != case_target or mutated:
                        return snapshot
                    mutated = True
                    if mutation == "content":
                        replacement = bytes(byte ^ 0xFF for byte in original_payload)
                        case_target.write_bytes(replacement)
                    elif mutation == "mode":
                        case_target.chmod(expected.mode ^ stat.S_IXUSR)
                    elif mutation == "link-count":
                        os.link(case_target, case_home / "same-inode-extra-link")
                    else:
                        return replace(snapshot, uid=snapshot.uid + 1)
                    return snapshot

                with (
                    mock.patch.object(
                        MODULE,
                        "_regular_file_snapshot_at",
                        side_effect=mutate_after_descriptor_capture,
                    ),
                    mock.patch.object(MODULE, "_rename_noreplace_at") as rename,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "canonical payload changed before evacuation",
                    ),
                ):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertTrue(mutated)
                rename.assert_not_called()
                self.assertTrue(ticket.path.is_file())
                self.assertTrue(case_target.is_file())
                self.assertEqual(
                    (case_target.stat().st_dev, case_target.stat().st_ino),
                    expected.file_identity,
                )

    def test_v6_alias_evacuation_rejects_same_inode_content_change(self) -> None:
        case_home = self.root / "ephemeral-v6-alias-content-change"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        alias_names = MODULE._pending_ephemeral_public_alias_names(
            ticket.batch_root.name
        )
        foreign_alias = case_target.with_name(alias_names[0])
        original_payload = b"same-uid competing alias\n"
        changed_payload = b"same-uid changed-- alias\n"
        self.assertEqual(len(original_payload), len(changed_payload))
        foreign_alias.write_bytes(original_payload)
        foreign_alias.chmod(0o600)
        foreign_identity = (
            foreign_alias.stat().st_dev,
            foreign_alias.stat().st_ino,
        )
        real_snapshot = MODULE._regular_file_snapshot_at
        mutated = False

        def mutate_alias_after_descriptor_capture(
            parent_fd: int,
            name: str,
            path: Path,
            *,
            maximum_bytes: int = MODULE.MAX_ARCHIVE_MEMBER_BYTES,
        ):
            nonlocal mutated
            snapshot = real_snapshot(
                parent_fd,
                name,
                path,
                maximum_bytes=maximum_bytes,
            )
            if path == foreign_alias and not mutated:
                mutated = True
                foreign_alias.write_bytes(changed_payload)
            return snapshot

        with (
            mock.patch.object(
                MODULE,
                "_regular_file_snapshot_at",
                side_effect=mutate_alias_after_descriptor_capture,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "alias payload changed before evacuation",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(mutated)
        self.assertFalse(os.path.lexists(case_target))
        self.assertEqual(foreign_alias.read_bytes(), changed_payload)
        self.assertEqual(
            (foreign_alias.stat().st_dev, foreign_alias.stat().st_ino),
            foreign_identity,
        )
        exact_alias = case_target.with_name(alias_names[1])
        self.assertEqual(
            (exact_alias.stat().st_dev, exact_alias.stat().st_ino),
            expected.file_identity,
        )
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_directory_beneath(case_home, quarantine_root)
        try:
            self.assertEqual(
                MODULE._pending_ephemeral_quarantine_private_inventory(
                    quarantine_fd,
                    ticket.batch_root.name,
                ),
                (),
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        self.assertTrue(ticket.path.is_file())

    def test_v6_ephemeral_cleanup_restart_after_private_unlink_preserves_foreign_canonical(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-post-unlink-reappearance"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            self._receiptless_cleanup_crash_patch("private-unlink-after"),
            self.assertRaisesRegex(SystemExit, "injected crash after private unlink"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        ticket = self._only_cleanup_ticket_for_home(case_home)
        phase_receipt = MODULE._read_pending_cleanup_terminal_validation(
            case_home,
            ticket,
            ticket.quarantine_root_identity,
        )
        self.assertIsNotNone(phase_receipt)
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        self.assertFalse(tuple(quarantine_root.glob(".codex-ephemeral-cleanup-*")))

        case_target.write_bytes(b"foreign after private unlink\n")
        case_target.chmod(0o600)
        protected_identity = (
            case_target.stat().st_dev,
            case_target.stat().st_ino,
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "public canonical reappeared after private isolation",
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertEqual(case_target.read_bytes(), b"foreign after private unlink\n")
        self.assertEqual(
            (case_target.stat().st_dev, case_target.stat().st_ino),
            protected_identity,
        )
        self.assertTrue(ticket.path.is_file())
        self.assertIsNotNone(
            MODULE._read_pending_cleanup_terminal_validation(
                case_home,
                ticket,
                ticket.quarantine_root_identity,
            )
        )

    def test_v6_final_private_unlink_preserves_boundary_replacement(self) -> None:
        case_home = self.root / "ephemeral-v6-final-private-replacement"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            self._receiptless_cleanup_crash_patch("alias-private"),
            self.assertRaisesRegex(SystemExit, "after private isolation"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        ticket = self._only_cleanup_ticket_for_home(case_home)
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        protected_exact = quarantine_root / "protected-exact-private-payload"
        replacement_path: Path | None = None
        replacement_identity: tuple[int, int] | None = None
        final_private_policy_checks = 0
        real_require = MODULE._require_release_identity_fd_access_policy

        def replace_after_bound_fd_policy(
            file_descriptor: int,
            display_path: Path,
            expected_owner_uid: int,
        ):
            nonlocal replacement_path
            nonlocal replacement_identity
            nonlocal final_private_policy_checks
            result = real_require(
                file_descriptor,
                display_path,
                expected_owner_uid,
            )
            if ".delete-" not in display_path.name:
                return result
            final_private_policy_checks += 1
            if final_private_policy_checks != 3:
                return result
            display_path.rename(protected_exact)
            display_path.write_bytes(b"foreign final-private replacement\n")
            display_path.chmod(0o600)
            metadata = display_path.stat()
            replacement_path = display_path
            replacement_identity = (metadata.st_dev, metadata.st_ino)
            return result

        with (
            mock.patch.object(
                MODULE,
                "_require_release_identity_fd_access_policy",
                side_effect=replace_after_bound_fd_policy,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "final private payload changed before deletion",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertGreaterEqual(final_private_policy_checks, 3)
        self.assertIsNotNone(replacement_path)
        assert replacement_path is not None
        self.assertTrue(replacement_path.is_file())
        self.assertEqual(
            (replacement_path.stat().st_dev, replacement_path.stat().st_ino),
            replacement_identity,
        )
        self.assertEqual(
            replacement_path.read_bytes(),
            b"foreign final-private replacement\n",
        )
        self.assertTrue(protected_exact.is_file())
        self.assertEqual(
            (protected_exact.stat().st_dev, protected_exact.stat().st_ino),
            expected.file_identity,
        )
        self.assertTrue(ticket.path.is_file())
        self.assertIsNotNone(
            MODULE._read_pending_cleanup_terminal_validation(
                case_home,
                ticket,
                ticket.quarantine_root_identity,
            )
        )

    def test_v6_ephemeral_cleanup_recovers_final_private_tombstone(self) -> None:
        case_home = self.root / "ephemeral-v6-final-private-crash"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        real_rename = MODULE._rename_noreplace_at
        crashed = False

        def crash_after_final_private_rename(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal crashed
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if ".delete-" in destination_name:
                crashed = True
                raise SystemExit("injected crash after final private isolation")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_final_private_rename,
            ),
            self.assertRaisesRegex(SystemExit, "final private isolation"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        self.assertTrue(crashed)
        ticket = self._only_cleanup_ticket_for_home(case_home)
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_directory_beneath(case_home, quarantine_root)
        try:
            private = MODULE._pending_ephemeral_quarantine_private_inventory(
                quarantine_fd,
                ticket.batch_root.name,
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0][1], expected.file_identity)
        self.assertIsNotNone(
            MODULE._pending_ephemeral_quarantine_final_private_base(
                ticket.batch_root.name,
                private[0][0],
            )
        )

        self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(case_target.exists())
        self.assertFalse(
            MODULE._pending_cleanup_terminal_validation_path(
                case_home,
                ticket.batch_root.name,
            ).exists()
        )
        quarantine_fd = MODULE._open_directory_beneath(case_home, quarantine_root)
        try:
            self.assertEqual(
                MODULE._pending_ephemeral_quarantine_private_inventory(
                    quarantine_fd,
                    ticket.batch_root.name,
                ),
                (),
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)

    def test_v6_final_private_unlink_requires_unchanged_phase_receipt(self) -> None:
        case_home = self.root / "ephemeral-v6-phase-receipt-boundary"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            self._receiptless_cleanup_crash_patch("alias-private"),
            self.assertRaisesRegex(SystemExit, "after private isolation"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        ticket = self._only_cleanup_ticket_for_home(case_home)
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            case_home,
            ticket.batch_root.name,
        )
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        real_require_ticket = MODULE._require_pending_cleanup_ticket_unchanged
        tombstone_ticket_checks = 0
        removed_receipt = False

        def remove_receipt_after_phase_binding(home: Path, current_ticket) -> None:
            nonlocal tombstone_ticket_checks, removed_receipt
            real_require_ticket(home, current_ticket)
            tombstones = tuple(
                path
                for path in quarantine_root.iterdir()
                if MODULE._pending_ephemeral_quarantine_final_private_base(
                    ticket.batch_root.name,
                    path.name,
                )
                is not None
            )
            if not tombstones:
                return
            tombstone_ticket_checks += 1
            if tombstone_ticket_checks == 2:
                receipt_path.unlink()
                removed_receipt = True

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_ticket_unchanged",
                side_effect=remove_receipt_after_phase_binding,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "private-phase receipt changed before payload deletion",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertTrue(removed_receipt)
        self.assertFalse(receipt_path.exists())
        self.assertTrue(ticket.path.is_file())
        quarantine_fd = MODULE._open_directory_beneath(case_home, quarantine_root)
        try:
            private = MODULE._pending_ephemeral_quarantine_private_inventory(
                quarantine_fd,
                ticket.batch_root.name,
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0][1], expected.file_identity)
        self.assertIsNotNone(
            MODULE._pending_ephemeral_quarantine_final_private_base(
                ticket.batch_root.name,
                private[0][0],
            )
        )

    def test_v6_terminal_receipt_rechecks_public_names_after_ticket_delete(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-terminal-replay"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        replay_source = case_home / "retained-payload-link"
        os.link(case_target, replay_source)
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        real_delete_ticket = MODULE._delete_pending_cleanup_ticket
        deleted_ticket = None

        def delete_ticket_then_replay(home: Path, ticket) -> None:
            nonlocal deleted_ticket
            real_delete_ticket(home, ticket)
            deleted_ticket = ticket
            os.link(replay_source, case_target)

        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_ticket",
                side_effect=delete_ticket_then_replay,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "public entry was retained in place",
            ),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        self.assertIsNotNone(deleted_ticket)
        assert deleted_ticket is not None
        self.assertFalse(deleted_ticket.path.exists())
        self.assertEqual(
            (case_target.stat().st_dev, case_target.stat().st_ino),
            expected.file_identity,
        )
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            case_home,
            deleted_ticket.batch_root.name,
        )
        self.assertTrue(receipt_path.is_file())

    def test_v6_terminal_receipt_rejects_retained_ticket_representation(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-terminal-retained-ticket"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        real_delete_ticket = MODULE._delete_pending_cleanup_ticket
        retained_ticket: Path | None = None
        deleted_ticket = None

        def retain_ticket_then_delete(home: Path, ticket) -> None:
            nonlocal retained_ticket, deleted_ticket
            retained_name = next(MODULE._retained_pending_cleanup_names(ticket.path))
            retained_ticket = ticket.path.with_name(retained_name)
            os.link(ticket.path, retained_ticket)
            real_delete_ticket(home, ticket)
            deleted_ticket = ticket

        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_ticket",
                side_effect=retain_ticket_then_delete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "ticket representation remained",
            ),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        self.assertIsNotNone(deleted_ticket)
        self.assertIsNotNone(retained_ticket)
        assert deleted_ticket is not None
        assert retained_ticket is not None
        self.assertFalse(deleted_ticket.path.exists())
        self.assertTrue(retained_ticket.is_file())
        self.assertEqual(retained_ticket.read_bytes(), deleted_ticket.snapshot.payload)
        self.assertTrue(
            MODULE._pending_cleanup_terminal_validation_path(
                case_home,
                deleted_ticket.batch_root.name,
            ).is_file()
        )

    def test_v6_terminal_receipt_rejects_malformed_ticket_tombstone(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-terminal-malformed-ticket"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        real_delete_ticket = MODULE._delete_pending_cleanup_ticket
        malformed_ticket: Path | None = None
        deleted_ticket = None

        def retain_malformed_ticket_then_delete(home: Path, ticket) -> None:
            nonlocal malformed_ticket, deleted_ticket
            retained_name = next(MODULE._retained_pending_cleanup_names(ticket.path))
            malformed_ticket = ticket.path.with_name(retained_name + ".extra")
            os.link(ticket.path, malformed_ticket)
            real_delete_ticket(home, ticket)
            deleted_ticket = ticket

        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_ticket",
                side_effect=retain_malformed_ticket_then_delete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "ticket representation remained",
            ),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        self.assertIsNotNone(deleted_ticket)
        self.assertIsNotNone(malformed_ticket)
        assert deleted_ticket is not None
        assert malformed_ticket is not None
        self.assertFalse(deleted_ticket.path.exists())
        self.assertTrue(malformed_ticket.is_file())
        self.assertEqual(malformed_ticket.read_bytes(), deleted_ticket.snapshot.payload)

        self.assertEqual(
            (malformed_ticket.stat().st_dev, malformed_ticket.stat().st_ino),
            deleted_ticket.snapshot.file_identity,
        )
        receipt_path = MODULE._pending_cleanup_terminal_validation_path(
            case_home,
            deleted_ticket.batch_root.name,
        )
        self.assertTrue(receipt_path.is_file())

        # This simulates the formerly unsafe outcome: the phase receipt was
        # retired while a suffixed ticket tombstone still retained its bytes.
        # That residue must independently block a later mutation.
        receipt_path.unlink()
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "ticket representation must be reconciled before new mutation",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(case_home)

        self.assertTrue(malformed_ticket.is_file())
        self.assertEqual(malformed_ticket.read_bytes(), deleted_ticket.snapshot.payload)

    def test_v5_v7_empty_proof_survives_ticket_representation_after_delete(
        self,
    ) -> None:
        for version, representation_kind in ((5, "retained"), (7, "suffixed")):
            with self.subTest(
                ticket_version=version,
                representation_kind=representation_kind,
            ):
                case_home = self.root / (
                    f"ephemeral-v{version}-{representation_kind}-ticket-proof"
                )
                if version == 5:
                    install(self.first_release, case_home, SHA_A)
                    ticket = self._make_v5_empty_ticket(
                        case_home,
                        case_home / ROLE_TARGET,
                    )
                else:
                    case_home.mkdir()
                    allocation = MODULE._quarantine_batch_root(
                        case_home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                    self.assertIsInstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    assert isinstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                        case_home,
                        allocation.binding,
                    )
                    allocation.revoke_reclaim()
                    allocation.close()

                proof_path = MODULE._pending_cleanup_empty_proof_path(
                    case_home,
                    ticket.batch_root.name,
                )
                retained_name = next(
                    MODULE._retained_pending_cleanup_names(ticket.path)
                )
                if representation_kind == "suffixed":
                    retained_name += ".extra"
                representation = ticket.path.with_name(retained_name)
                ticket_payload = ticket.snapshot.payload
                real_delete_ticket = MODULE._delete_pending_cleanup_ticket
                represented = False

                def represent_then_delete(home: Path, current_ticket) -> None:
                    nonlocal represented
                    if current_ticket.path == ticket.path and not represented:
                        os.link(current_ticket.path, representation)
                        represented = True
                    real_delete_ticket(home, current_ticket)

                with (
                    mock.patch.object(
                        MODULE,
                        "_delete_pending_cleanup_ticket",
                        side_effect=represent_then_delete,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "ticket representation",
                    ),
                ):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertTrue(represented)
                self.assertFalse(ticket.path.exists())
                self.assertFalse(ticket.batch_root.exists())
                self.assertTrue(representation.is_file())
                self.assertEqual(representation.read_bytes(), ticket_payload)
                self.assertTrue(proof_path.is_file())

                if representation_kind == "retained":
                    self.assertEqual(
                        MODULE._cleanup_ready_pending_batches(case_home),
                        1,
                    )
                    self.assertFalse(representation.exists())
                    self.assertFalse(proof_path.exists())
                else:
                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "ticket representation",
                    ):
                        MODULE._cleanup_ready_pending_batches(case_home)
                    self.assertTrue(representation.is_file())
                    self.assertEqual(representation.read_bytes(), ticket_payload)
                    self.assertTrue(proof_path.is_file())

    def test_v7_empty_proof_final_unlink_rechecks_ticket_representations(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v7-proof-final-unlink"
        case_home.mkdir()
        allocation = MODULE._quarantine_batch_root(
            case_home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        self.assertIsInstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        assert isinstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            case_home,
            allocation.binding,
        )
        allocation.revoke_reclaim()
        allocation.close()

        proof_path = MODULE._pending_cleanup_empty_proof_path(
            case_home,
            ticket.batch_root.name,
        )
        malformed_ticket = ticket.path.with_name(ticket.path.name + ".late")
        real_require = MODULE._require_pending_ephemeral_ticket_representations_absent
        boundary_checks = 0

        def inject_ticket_at_final_proof_unlink(
            home: Path,
            index_root: Path,
            index_fd: int,
            batch_name: str,
        ) -> None:
            nonlocal boundary_checks
            boundary_checks += 1
            if boundary_checks == 3:
                malformed_ticket.write_bytes(ticket.snapshot.payload or b"")
                malformed_ticket.chmod(0o600)
            real_require(
                home,
                index_root,
                index_fd,
                batch_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_ephemeral_ticket_representations_absent",
                side_effect=inject_ticket_at_final_proof_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "ticket representation remained",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertEqual(boundary_checks, 3)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(ticket.batch_root.exists())
        self.assertTrue(malformed_ticket.is_file())
        self.assertFalse(proof_path.exists())
        retained_proofs = tuple(
            proof_path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{proof_path.name}-*"
            )
        )
        self.assertEqual(len(retained_proofs), 1)
        parsed_retained = MODULE._pending_cleanup_retained_control_name(
            retained_proofs[0].name
        )
        self.assertEqual(
            parsed_retained,
            (proof_path.name, ticket.batch_root.name),
        )
        self.assertEqual(retained_proofs[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            retained_proofs[0].read_bytes(),
            MODULE._pending_cleanup_empty_proof_payload(
                ticket,
                ticket.quarantine_root_identity,
            ),
        )

    def test_orphan_empty_proof_final_unlink_rechecks_ticket_representations(
        self,
    ) -> None:
        for representation_kind in ("canonical", "retained"):
            with self.subTest(representation_kind=representation_kind):
                case_home = self.root / (
                    f"orphan-empty-proof-{representation_kind}-ticket-replay"
                )
                case_home.mkdir()
                allocation = MODULE._quarantine_batch_root(
                    case_home,
                    [],
                    retain_binding=True,
                    retain_scaffold_binding=True,
                )
                self.assertIsInstance(
                    allocation,
                    MODULE.EphemeralQuarantineBatchAllocation,
                )
                assert isinstance(
                    allocation,
                    MODULE.EphemeralQuarantineBatchAllocation,
                )
                ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                    case_home,
                    allocation.binding,
                )
                allocation.revoke_reclaim()
                allocation.close()

                proof_path = MODULE._pending_cleanup_empty_proof_path(
                    case_home,
                    ticket.batch_root.name,
                )
                hold_path = case_home / "preserved-ticket-bytes"
                os.link(ticket.path, hold_path)
                self.assertEqual(
                    (hold_path.stat().st_dev, hold_path.stat().st_ino),
                    ticket.snapshot.file_identity,
                )
                # Preserve an orphan proof deliberately.  Production retires
                # the joined v8 allocation only after that proof is deleted;
                # suspend the matching final step here so this fixture models
                # the crash window without weakening the v8-only control gate.
                with (
                    mock.patch.object(
                        MODULE,
                        "_delete_pending_cleanup_empty_proof",
                        return_value=None,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_retire_joined_quarantine_allocation",
                        return_value=None,
                    ),
                ):
                    self.assertTrue(
                        MODULE._remove_cleanup_ready_batch(case_home, ticket)
                    )

                self.assertFalse(ticket.path.exists())
                self.assertFalse(ticket.batch_root.exists())
                self.assertTrue(proof_path.is_file())
                if representation_kind == "canonical":
                    replay_path = ticket.path
                else:
                    replay_path = ticket.path.with_name(
                        next(MODULE._retained_pending_cleanup_names(ticket.path))
                    )
                real_require = (
                    MODULE._require_pending_ephemeral_ticket_representations_absent
                )
                injected = False

                def replay_ticket_at_final_orphan_unlink(
                    home: Path,
                    index_root: Path,
                    index_fd: int,
                    batch_name: str,
                ) -> None:
                    nonlocal injected
                    retained_proofs = tuple(
                        index_root.glob(
                            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
                            f"{proof_path.name}-*"
                        )
                    )
                    if (
                        batch_name == ticket.batch_root.name
                        and not proof_path.exists()
                        and len(retained_proofs) == 1
                        and not injected
                    ):
                        os.link(hold_path, replay_path)
                        injected = True
                    real_require(home, index_root, index_fd, batch_name)

                with (
                    mock.patch.object(
                        MODULE,
                        "_require_pending_ephemeral_ticket_representations_absent",
                        side_effect=replay_ticket_at_final_orphan_unlink,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "ticket representation remained",
                    ),
                ):
                    MODULE._cleanup_orphan_pending_cleanup_empty_proofs(case_home)

                self.assertTrue(injected)
                self.assertTrue(replay_path.is_file())
                self.assertEqual(
                    (replay_path.stat().st_dev, replay_path.stat().st_ino),
                    ticket.snapshot.file_identity,
                )
                self.assertFalse(proof_path.exists())
                retained_proofs = tuple(
                    proof_path.parent.glob(
                        f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{proof_path.name}-*"
                    )
                )
                self.assertEqual(len(retained_proofs), 1)
                self.assertEqual(
                    retained_proofs[0].read_bytes(),
                    MODULE._pending_cleanup_empty_proof_payload(
                        ticket,
                        ticket.quarantine_root_identity,
                    ),
                )

                hold_path.unlink()
                self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)
                self.assertFalse(replay_path.exists())
                self.assertFalse(retained_proofs[0].exists())

    def test_v6_cleanup_closes_public_parent_when_quarantine_open_fails(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-quarantine-open-failure"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_leaf_cleanup_ticket(
            case_home,
            case_target,
            expected,
        )
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        public_parent_fd = 123

        def fail_second_directory_open(home: Path, directory: Path) -> int:
            self.assertEqual(home, case_home)
            if directory == case_target.parent:
                return public_parent_fd
            self.assertEqual(directory, quarantine_root)
            raise SystemExit("injected quarantine directory open failure")

        with (
            mock.patch.object(
                MODULE,
                "_open_directory_beneath",
                side_effect=fail_second_directory_open,
            ) as open_bound,
            mock.patch.object(MODULE, "_close_fd_quietly") as close_bound,
            self.assertRaisesRegex(SystemExit, "quarantine directory open failure"),
        ):
            MODULE._remove_pending_ephemeral_quarantine_leaf(case_home, ticket)

        open_bound.assert_has_calls(
            [
                mock.call(case_home, case_target.parent),
                mock.call(case_home, quarantine_root),
            ]
        )
        close_bound.assert_any_call(public_parent_fd)
        self.assertTrue(ticket.path.is_file())

    def test_v6_ephemeral_cleanup_orphan_phase_preserves_foreign_after_ticket_delete(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-orphan-private-phase"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_terminal_validation",
                side_effect=SystemExit("injected crash before phase deletion"),
            ),
            self.assertRaisesRegex(SystemExit, "before phase deletion"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        index_root = MODULE._pending_cleanup_index_path(case_home)
        self.assertFalse(tuple(index_root.glob("*.json")))
        phase_receipts = tuple(
            index_root.glob(f"*{MODULE.PENDING_CLEANUP_TERMINAL_VALIDATION_SUFFIX}")
        )
        self.assertEqual(len(phase_receipts), 1)
        case_target.write_bytes(b"foreign after ticket deletion\n")
        case_target.chmod(0o600)
        protected_identity = (
            case_target.stat().st_dev,
            case_target.stat().st_ino,
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "public entry was retained in place",
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertEqual(case_target.read_bytes(), b"foreign after ticket deletion\n")
        self.assertEqual(
            (case_target.stat().st_dev, case_target.stat().st_ino),
            protected_identity,
        )
        self.assertTrue(phase_receipts[0].is_file())

        case_target.unlink()
        self.assertEqual(MODULE._cleanup_ready_pending_batches(case_home), 1)
        self.assertFalse(phase_receipts[0].exists())

    def test_v6_orphan_receipt_rechecks_ticket_forms_at_mutation_boundary(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v6-orphan-ticket-replay"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        expected = MODULE._read_regular_file_snapshot_beneath(
            case_home,
            case_target,
            require_managed_access=False,
        )
        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_terminal_validation",
                side_effect=SystemExit("injected crash before phase deletion"),
            ),
            self.assertRaisesRegex(SystemExit, "before phase deletion"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                case_home,
                case_target,
                expected,
            )

        index_root = MODULE._pending_cleanup_index_path(case_home)
        phase_receipts = tuple(
            index_root.glob(f"*{MODULE.PENDING_CLEANUP_TERMINAL_VALIDATION_SUFFIX}")
        )
        self.assertEqual(len(phase_receipts), 1)
        batch_name = phase_receipts[0].name[
            : -len(MODULE.PENDING_CLEANUP_TERMINAL_VALIDATION_SUFFIX)
        ]
        ticket_path = MODULE._pending_cleanup_ticket_path(case_home, batch_name)
        self.assertFalse(ticket_path.exists())
        ticket_temp_name = batch_name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        retained_ticket_temp = index_root / (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{ticket_temp_name}-"
            f"{os.getpid()}-{'a' * 16}"
        )
        real_require = MODULE._require_pending_cleanup_fd_access_policy
        index_policy_checks = 0

        def replay_ticket_during_receipt_deletion(
            file_descriptor: int,
            display_path: Path,
            *,
            expected_mode: int,
        ):
            nonlocal index_policy_checks
            result = real_require(
                file_descriptor,
                display_path,
                expected_mode=expected_mode,
            )
            if display_path != index_root:
                return result
            index_policy_checks += 1
            if index_policy_checks == 3:
                retained_ticket_temp.write_bytes(b"foreign replayed ticket temp\n")
                retained_ticket_temp.chmod(0o600)
            return result

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_fd_access_policy",
                side_effect=replay_ticket_during_receipt_deletion,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "ticket representation remained",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertEqual(index_policy_checks, 3)
        self.assertFalse(ticket_path.exists())
        self.assertEqual(
            retained_ticket_temp.read_bytes(),
            b"foreign replayed ticket temp\n",
        )
        self.assertTrue(phase_receipts[0].is_file())

    def test_v5_ephemeral_cleanup_preserves_foreign_or_replaced_members(
        self,
    ) -> None:
        for case in ("foreign-sibling", "metadata-replacement", "dual-roots"):
            with self.subTest(case=case):
                case_home = self.root / f"ephemeral-{case}"
                install(self.first_release, case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                ticket = self._make_v5_empty_ticket(
                    case_home,
                    case_target,
                )

                if case == "foreign-sibling":
                    protected = ticket.batch_root / "foreign"
                    protected.write_bytes(b"foreign sibling")
                    expected_error = "unknown member"
                elif case == "metadata-replacement":
                    protected = ticket.batch_root / "metadata.json"
                    protected.unlink()
                    protected.write_bytes(b"foreign metadata")
                    protected.chmod(0o600)
                    expected_error = "metadata"
                else:
                    protected = ticket.batch_root.with_name(ticket.isolated_name or "")
                    protected.mkdir(mode=0o700)
                    expected_error = "canonical and isolated roots"
                protected_identity = (
                    protected.stat().st_dev,
                    protected.stat().st_ino,
                )
                protected_payload = (
                    protected.read_bytes() if protected.is_file() else None
                )

                with self.assertRaisesRegex(MODULE.SyncError, expected_error):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertTrue(ticket.path.is_file())
                self.assertTrue(protected.exists())
                self.assertEqual(
                    (protected.stat().st_dev, protected.stat().st_ino),
                    protected_identity,
                )
                if protected_payload is not None:
                    self.assertEqual(protected.read_bytes(), protected_payload)

    def test_v5_ephemeral_cleanup_revalidates_ticket_control_properties(
        self,
    ) -> None:
        for changed_property in ("identity", "content", "access-policy"):
            with self.subTest(changed_property=changed_property):
                case_home = self.root / f"ephemeral-ticket-{changed_property}"
                install(self.first_release, case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                ticket = self._make_v5_empty_ticket(
                    case_home,
                    case_target,
                )
                ticket_payload = ticket.path.read_bytes()
                real_members = MODULE._pending_ephemeral_batch_members
                changed = False

                def change_ticket_after_inventory(batch_fd: int, batch_name: str):
                    nonlocal changed
                    result = real_members(batch_fd, batch_name)
                    if changed:
                        return result
                    changed = True
                    if changed_property == "identity":
                        original_ticket_fd = os.open(ticket.path, os.O_RDONLY)
                        try:
                            ticket.path.unlink()
                            ticket.path.write_bytes(ticket_payload)
                            ticket.path.chmod(0o600)
                        finally:
                            os.close(original_ticket_fd)
                    elif changed_property == "content":
                        changed_payload = ticket_payload.replace(
                            b'"leaf"',
                            b'"leAf"',
                            1,
                        )
                        self.assertNotEqual(changed_payload, ticket_payload)
                        ticket.path.write_bytes(changed_payload)
                    else:
                        ticket.path.chmod(0o640)
                    return result

                with (
                    mock.patch.object(
                        MODULE,
                        "_pending_ephemeral_batch_members",
                        side_effect=change_ticket_after_inventory,
                    ),
                    self.assertRaises(MODULE.SyncError),
                ):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertTrue(changed)
                self.assertTrue(ticket.batch_root.is_dir())
                self.assertTrue(ticket.path.is_file())

    def test_v5_v7_metadata_unlink_rechecks_same_inode_after_callback(self) -> None:
        for version in (5, 7):
            with self.subTest(ticket_version=version):
                case_home = self.root / f"ephemeral-v{version}-metadata-in-place"
                if version == 5:
                    install(self.first_release, case_home, SHA_A)
                    ticket = self._make_v5_empty_ticket(
                        case_home,
                        case_home / ROLE_TARGET,
                    )
                else:
                    case_home.mkdir()
                    allocation = MODULE._quarantine_batch_root(
                        case_home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                    self.assertIsInstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    assert isinstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                        case_home,
                        allocation.binding,
                    )
                    allocation.revoke_reclaim()
                    allocation.close()

                real_boundary = (
                    MODULE._require_pending_ephemeral_metadata_cleanup_boundary
                )
                changed_path: Path | None = None
                changed_identity: tuple[int, int] | None = None
                changed_payload: bytes | None = None

                def mutate_same_inode_after_boundary(
                    home: Path,
                    current_ticket,
                    quarantine_root: Path,
                    quarantine_fd: int,
                    bound_batch_root: Path,
                    bound_name: str,
                    batch_fd: int,
                    metadata_name: str,
                ) -> None:
                    nonlocal changed_path, changed_identity, changed_payload
                    real_boundary(
                        home,
                        current_ticket,
                        quarantine_root,
                        quarantine_fd,
                        bound_batch_root,
                        bound_name,
                        batch_fd,
                        metadata_name,
                    )
                    if metadata_name == "metadata.json" or changed_path is not None:
                        return
                    retained_path = bound_batch_root / metadata_name
                    original = retained_path.read_bytes()
                    replacement = bytes([original[0] ^ 1]) + original[1:]
                    before = retained_path.stat()
                    with retained_path.open("r+b") as retained_file:
                        retained_file.write(replacement)
                        retained_file.truncate()
                        retained_file.flush()
                        os.fsync(retained_file.fileno())
                    after = retained_path.stat()
                    self.assertEqual(
                        (after.st_dev, after.st_ino),
                        (before.st_dev, before.st_ino),
                    )
                    changed_path = retained_path
                    changed_identity = (after.st_dev, after.st_ino)
                    changed_payload = replacement

                with (
                    mock.patch.object(
                        MODULE,
                        "_require_pending_ephemeral_metadata_cleanup_boundary",
                        side_effect=mutate_same_inode_after_boundary,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "after mutation revalidation",
                    ),
                ):
                    MODULE._cleanup_ready_pending_batches(case_home)

                self.assertIsNotNone(changed_path)
                assert changed_path is not None
                self.assertTrue(changed_path.is_file())
                self.assertEqual(
                    (changed_path.stat().st_dev, changed_path.stat().st_ino),
                    changed_identity,
                )
                self.assertEqual(changed_path.read_bytes(), changed_payload)
                self.assertTrue(ticket.path.is_file())

    def test_v5_v7_batch_isolation_rechecks_quarantine_root_policy(self) -> None:
        for version in (5, 7):
            with self.subTest(ticket_version=version):
                case_home = self.root / f"ephemeral-v{version}-root-policy-boundary"
                if version == 5:
                    install(self.first_release, case_home, SHA_A)
                    ticket = self._make_v5_empty_ticket(
                        case_home,
                        case_home / ROLE_TARGET,
                    )
                else:
                    case_home.mkdir()
                    allocation = MODULE._quarantine_batch_root(
                        case_home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                    self.assertIsInstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    assert isinstance(
                        allocation,
                        MODULE.EphemeralQuarantineBatchAllocation,
                    )
                    ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                        case_home,
                        allocation.binding,
                    )
                    allocation.revoke_reclaim()
                    allocation.close()

                quarantine_root = ticket.batch_root.parent
                isolated = ticket.batch_root.with_name(ticket.isolated_name or "")
                proof_path = MODULE._pending_cleanup_empty_proof_path(
                    case_home,
                    ticket.batch_root.name,
                )
                real_require = MODULE._require_pending_cleanup_fd_access_policy
                drifted = False

                def drift_root_after_empty_batch_policy(
                    file_descriptor: int,
                    display_path: Path,
                    *,
                    expected_mode: int,
                ):
                    nonlocal drifted
                    result = real_require(
                        file_descriptor,
                        display_path,
                        expected_mode=expected_mode,
                    )
                    if (
                        not drifted
                        and display_path == ticket.batch_root
                        and ticket.batch_root.is_dir()
                        and not tuple(ticket.batch_root.iterdir())
                    ):
                        quarantine_root.chmod(0o770)
                        drifted = True
                    return result

                try:
                    with (
                        mock.patch.object(
                            MODULE,
                            "_require_pending_cleanup_fd_access_policy",
                            side_effect=drift_root_after_empty_batch_policy,
                        ),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "access policy mismatch",
                        ),
                    ):
                        MODULE._cleanup_ready_pending_batches(case_home)

                    self.assertTrue(drifted)
                    self.assertTrue(ticket.batch_root.is_dir())
                    self.assertFalse(isolated.exists())
                    self.assertFalse(proof_path.exists())
                    self.assertTrue(ticket.path.is_file())
                finally:
                    quarantine_root.chmod(0o700)

    @unittest.skipUnless(sys.platform == "darwin", "requires Darwin ACLs")
    def test_v7_batch_isolation_rechecks_quarantine_root_acl(self) -> None:
        case_home = self.root / "ephemeral-v7-root-acl-boundary"
        case_home.mkdir()
        allocation = MODULE._quarantine_batch_root(
            case_home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        self.assertIsInstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        assert isinstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            case_home,
            allocation.binding,
        )
        allocation.revoke_reclaim()
        allocation.close()
        quarantine_root = ticket.batch_root.parent
        isolated = ticket.batch_root.with_name(ticket.isolated_name or "")
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            case_home,
            ticket.batch_root.name,
        )
        subprocess.run(
            ["/bin/chmod", "-N", os.fspath(quarantine_root)],
            check=True,
            capture_output=True,
            text=True,
        )
        real_require = MODULE._require_pending_cleanup_fd_access_policy
        drifted = False

        def drift_root_acl_after_empty_batch_policy(
            file_descriptor: int,
            display_path: Path,
            *,
            expected_mode: int,
        ):
            nonlocal drifted
            result = real_require(
                file_descriptor,
                display_path,
                expected_mode=expected_mode,
            )
            if (
                not drifted
                and display_path == ticket.batch_root
                and ticket.batch_root.is_dir()
                and not tuple(ticket.batch_root.iterdir())
            ):
                subprocess.run(
                    [
                        "/bin/chmod",
                        "+a",
                        "everyone allow read",
                        os.fspath(quarantine_root),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                drifted = True
            return result

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_require_pending_cleanup_fd_access_policy",
                    side_effect=drift_root_acl_after_empty_batch_policy,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "grants ALLOW access to a non-owner qualifier",
                ),
            ):
                MODULE._cleanup_ready_pending_batches(case_home)

            self.assertTrue(drifted)
            self.assertTrue(ticket.batch_root.is_dir())
            self.assertFalse(isolated.exists())
            self.assertFalse(proof_path.exists())
            self.assertTrue(ticket.path.is_file())
        finally:
            subprocess.run(
                ["/bin/chmod", "-N", os.fspath(quarantine_root)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_v8_ticket_unlink_rechecks_same_inode_after_callback(self) -> None:
        case_home = self.root / "ephemeral-v8-ticket-in-place"
        case_home.mkdir()
        quarantine_root = (
            MODULE._personal_sync_root(case_home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            case_home,
            quarantine_root,
            mode=0o700,
        )
        batch_name = "20260905T000000Z-1-1"
        try:
            ticket = MODULE._publish_pending_quarantine_allocation_ticket(
                case_home,
                quarantine_root,
                quarantine_fd,
                batch_name,
                b"allocation metadata fixture\n",
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)

        changed_path: Path | None = None
        changed_identity: tuple[int, int] | None = None
        changed_payload: bytes | None = None

        def mutate_same_inode_after_boundary() -> None:
            nonlocal changed_path, changed_identity, changed_payload
            if ticket.path.exists() or changed_path is not None:
                return
            retained = tuple(
                ticket.path.parent.glob(
                    f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{ticket.path.name}-*"
                )
            )
            self.assertEqual(len(retained), 1)
            retained_path = retained[0]
            original = retained_path.read_bytes()
            replacement = bytes([original[0] ^ 1]) + original[1:]
            before = retained_path.stat()
            with retained_path.open("r+b") as retained_file:
                retained_file.write(replacement)
                retained_file.truncate()
                retained_file.flush()
                os.fsync(retained_file.fileno())
            after = retained_path.stat()
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (before.st_dev, before.st_ino),
            )
            changed_path = retained_path
            changed_identity = (after.st_dev, after.st_ino)
            changed_payload = replacement

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "after mutation revalidation",
        ):
            MODULE._delete_pending_quarantine_allocation_ticket(
                case_home,
                ticket,
                boundary_revalidator=mutate_same_inode_after_boundary,
            )

        self.assertIsNotNone(changed_path)
        assert changed_path is not None
        self.assertTrue(changed_path.is_file())
        self.assertEqual(
            (changed_path.stat().st_dev, changed_path.stat().st_ino),
            changed_identity,
        )
        self.assertEqual(changed_path.read_bytes(), changed_payload)
        self.assertFalse(ticket.path.exists())

    def test_v5_ephemeral_cleanup_revalidates_leaf_at_rmdir_boundary(self) -> None:
        case_home = self.root / "ephemeral-v5-leaf-rmdir-boundary"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        ticket = self._make_v5_empty_ticket(case_home, case_target)
        leaf = ticket.batch_root / "leaf"
        real_require = MODULE._require_pending_cleanup_fd_access_policy
        leaf_checks = 0
        replacement_identity: tuple[int, int] | None = None

        def replace_leaf_on_final_access(
            file_descriptor: int,
            display_path: Path,
            *,
            expected_mode: int,
        ):
            nonlocal leaf_checks, replacement_identity
            result = real_require(
                file_descriptor,
                display_path,
                expected_mode=expected_mode,
            )
            if display_path == leaf:
                leaf_checks += 1
                if leaf_checks == 2:
                    original_leaf_fd = os.open(
                        leaf,
                        MODULE._directory_open_flags(nofollow=True),
                    )
                    try:
                        leaf.rmdir()
                        leaf.mkdir(mode=0o700)
                        protected = leaf / "foreign"
                        protected.write_bytes(b"foreign leaf\n")
                        replacement = leaf.stat()
                        replacement_identity = (
                            replacement.st_dev,
                            replacement.st_ino,
                        )
                    finally:
                        os.close(original_leaf_fd)
            return result

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_fd_access_policy",
                side_effect=replace_leaf_on_final_access,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "leaf changed before removal"),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertEqual(leaf_checks, 2)
        self.assertTrue(ticket.path.is_file())
        self.assertEqual((leaf.stat().st_dev, leaf.stat().st_ino), replacement_identity)
        self.assertEqual((leaf / "foreign").read_bytes(), b"foreign leaf\n")

    def test_v5_ephemeral_cleanup_revalidates_names_at_batch_rmdir_boundary(
        self,
    ) -> None:
        case_home = self.root / "ephemeral-v5-batch-rmdir-boundary"
        install(self.first_release, case_home, SHA_A)
        case_target = case_home / ROLE_TARGET
        ticket = self._make_v5_empty_ticket(case_home, case_target)
        isolated = ticket.batch_root.with_name(ticket.isolated_name or "")
        real_require = MODULE._require_pending_cleanup_ticket_unchanged
        replacement_identity: tuple[int, int] | None = None

        def recreate_canonical_before_final_rmdir(home: Path, current) -> None:
            nonlocal replacement_identity
            real_require(home, current)
            if (
                replacement_identity is None
                and isolated.is_dir()
                and not ticket.batch_root.exists()
            ):
                ticket.batch_root.mkdir(mode=0o700)
                protected = ticket.batch_root / "foreign"
                protected.write_bytes(b"foreign batch\n")
                replacement = ticket.batch_root.stat()
                replacement_identity = (replacement.st_dev, replacement.st_ino)

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_ticket_unchanged",
                side_effect=recreate_canonical_before_final_rmdir,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "batch changed before removal"),
        ):
            MODULE._cleanup_ready_pending_batches(case_home)

        self.assertIsNotNone(replacement_identity)
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(isolated.is_dir())
        self.assertEqual(
            (ticket.batch_root.stat().st_dev, ticket.batch_root.stat().st_ino),
            replacement_identity,
        )
        self.assertEqual(
            (ticket.batch_root / "foreign").read_bytes(),
            b"foreign batch\n",
        )

    def test_v1_v2_cleanup_backlog_does_not_block_terminal_authority_gate(
        self,
    ) -> None:
        for version in (1, 2):
            with self.subTest(ticket_version=version):
                ticket = self._publish_legacy_cleanup_ticket(version=version)
                MODULE._require_no_pending_terminal_mutation_authority(self.home)
                self.assertTrue(ticket.path.is_file())

    def test_malformed_ticket_representations_fail_closed_in_read_only_paths(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        batch_name = "20260901T000000Z-5-0"
        representations = (
            index_root / f"{batch_name}.json.extra",
            index_root
            / (
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{batch_name}.json-"
                "123-0000000000000001.extra"
            ),
        )

        for representation in representations:
            with self.subTest(representation=representation.name):
                representation.write_bytes(b"retained ticket evidence\n")
                representation.chmod(0o600)
                evidence_before = representation.read_bytes()

                status_output = io.StringIO()
                with contextlib.redirect_stdout(status_output):
                    healthy = MODULE.status(self.home)
                self.assertFalse(healthy)
                self.assertIn(
                    "pending cleanup ticket representation must be reconciled",
                    status_output.getvalue(),
                )

                install_output = io.StringIO()
                with (
                    contextlib.redirect_stdout(install_output),
                    mock.patch.object(MODULE, "_source_release_identity") as source,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "pending cleanup ticket representation must be reconciled",
                    ),
                ):
                    MODULE.install_release_tree(
                        self.next_release,
                        self.home,
                        SHA_B,
                        dry_run=True,
                    )
                source.assert_not_called()
                self.assertNotIn("would clean", install_output.getvalue())

                uninstall_output = io.StringIO()
                with (
                    contextlib.redirect_stdout(uninstall_output),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "pending cleanup ticket representation must be reconciled",
                    ),
                ):
                    MODULE.uninstall_overlay(
                        self.home,
                        "private",
                        dry_run=True,
                    )
                self.assertNotIn("would clean", uninstall_output.getvalue())
                self.assertTrue(representation.is_file())
                self.assertEqual(representation.read_bytes(), evidence_before)
                representation.unlink()

    def test_malformed_v8_control_representations_fail_closed_in_read_only_paths(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        batch_name = "20260901T000000Z-8-0"
        canonical_names = (
            batch_name + MODULE.PENDING_QUARANTINE_ALLOCATION_SUFFIX,
            batch_name + MODULE.PENDING_QUARANTINE_ALLOCATION_TEMP_SUFFIX,
            batch_name + MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX,
        )
        representations = tuple(
            index_root / representation_name
            for canonical_name in canonical_names
            for representation_name in (
                canonical_name + ".extra",
                (
                    f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{canonical_name}-"
                    "123-0000000000000001.extra"
                ),
            )
        )

        for representation in representations:
            with self.subTest(representation=representation.name):
                representation.write_bytes(b"malformed v8 control evidence\n")
                representation.chmod(0o600)
                before = representation.stat()
                identity_before = (before.st_dev, before.st_ino)
                evidence_before = representation.read_bytes()

                status_output = io.StringIO()
                with contextlib.redirect_stdout(status_output):
                    healthy = MODULE.status(self.home)
                self.assertFalse(healthy)
                self.assertIn(
                    "pending quarantine allocation representation must be reconciled",
                    status_output.getvalue(),
                )

                install_output = io.StringIO()
                with (
                    contextlib.redirect_stdout(install_output),
                    mock.patch.object(MODULE, "_source_release_identity") as source,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "pending quarantine allocation representation must be "
                        "reconciled",
                    ),
                ):
                    MODULE.install_release_tree(
                        self.next_release,
                        self.home,
                        SHA_B,
                        dry_run=True,
                    )
                source.assert_not_called()
                self.assertNotIn("would clean", install_output.getvalue())

                uninstall_output = io.StringIO()
                with (
                    contextlib.redirect_stdout(uninstall_output),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "pending quarantine allocation representation must be "
                        "reconciled",
                    ),
                ):
                    MODULE.uninstall_overlay(
                        self.home,
                        "private",
                        dry_run=True,
                    )
                self.assertNotIn("would clean", uninstall_output.getvalue())
                self.assertTrue(representation.is_file())
                after = representation.stat()
                self.assertEqual((after.st_dev, after.st_ino), identity_before)
                self.assertEqual(representation.read_bytes(), evidence_before)
                representation.unlink()

    def test_malformed_v8_control_representation_blocks_global_scans(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        batch_name = "20260901T000000Z-8-1"
        representation = index_root / (
            batch_name + MODULE.PENDING_QUARANTINE_ALLOCATION_SUFFIX + ".extra"
        )
        representation.write_bytes(b"malformed v8 allocation evidence\n")
        representation.chmod(0o600)
        before = representation.stat()
        identity_before = (before.st_dev, before.st_ino)
        evidence_before = representation.read_bytes()

        for scan in (
            MODULE._pending_cleanup_ready_batch_is_observed,
            MODULE._require_no_pending_terminal_mutation_authority,
            MODULE._pending_quarantine_allocation_batch_names,
        ):
            with (
                self.subTest(scan=scan.__name__),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending quarantine allocation representation must be reconciled",
                ),
            ):
                scan(self.home)

        self.assertTrue(representation.is_file())
        after = representation.stat()
        self.assertEqual((after.st_dev, after.st_ino), identity_before)
        self.assertEqual(representation.read_bytes(), evidence_before)

    def test_read_only_ready_observation_rechecks_late_malformed_v8_control(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        batch_name = "20260901T000000Z-8-2"
        representation = index_root / (
            batch_name + MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX + ".extra"
        )
        real_preflight_scan = (
            MODULE._require_no_pending_unresolved_ticket_representations
        )
        injected = False

        def scan_then_inject(home: Path) -> None:
            nonlocal injected
            real_preflight_scan(home)
            if not injected:
                representation.write_bytes(b"late malformed v8 control evidence\n")
                representation.chmod(0o600)
                injected = True

        install_output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_require_no_pending_unresolved_ticket_representations",
                side_effect=scan_then_inject,
            ),
            mock.patch.object(MODULE, "_source_release_identity") as source,
            contextlib.redirect_stdout(install_output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending quarantine allocation representation must be reconciled",
            ),
        ):
            MODULE.install_release_tree(
                self.next_release,
                self.home,
                SHA_B,
                dry_run=True,
            )
        source.assert_not_called()
        self.assertNotIn("would clean", install_output.getvalue())
        self.assertTrue(injected)
        self.assertTrue(representation.is_file())
        self.assertEqual(
            representation.read_bytes(),
            b"late malformed v8 control evidence\n",
        )

    def test_read_only_ready_observation_rechecks_late_ticket_representation(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        ready_batch = "20260901T000000Z-5-1"
        ready_marker = index_root / (
            ready_batch + MODULE.PENDING_CLEANUP_EMPTY_PROOF_SUFFIX
        )
        ready_marker.write_bytes(b"ordinary cleanup-ready marker\n")
        ready_marker.chmod(0o600)
        unresolved_batch = "20260901T000000Z-5-2"
        malformed_ticket = index_root / f"{unresolved_batch}.json.extra"
        real_observe = MODULE._pending_cleanup_unresolved_ticket_representation_issue

        def inject_after_first_blocker_observation():
            observations = 0

            def observe_then_inject(home: Path):
                nonlocal observations
                unresolved = real_observe(home)
                observations += 1
                if observations == 1:
                    malformed_ticket.write_bytes(b"late ticket evidence\n")
                    malformed_ticket.chmod(0o600)
                return unresolved

            return observe_then_inject

        status_output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_pending_cleanup_unresolved_ticket_representation_issue",
                side_effect=inject_after_first_blocker_observation(),
            ),
            contextlib.redirect_stdout(status_output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket representation must be reconciled",
            ),
        ):
            MODULE.status(self.home)
        self.assertNotIn(
            "finalized or interrupted pending transaction must be cleaned",
            status_output.getvalue(),
        )

        malformed_ticket.unlink()
        install_output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_pending_cleanup_unresolved_ticket_representation_issue",
                side_effect=inject_after_first_blocker_observation(),
            ),
            mock.patch.object(MODULE, "_source_release_identity") as source,
            contextlib.redirect_stdout(install_output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket representation must be reconciled",
            ),
        ):
            MODULE.install_release_tree(
                self.next_release,
                self.home,
                SHA_B,
                dry_run=True,
            )
        source.assert_not_called()
        self.assertNotIn("would clean", install_output.getvalue())

        malformed_ticket.unlink()
        uninstall_output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_pending_cleanup_unresolved_ticket_representation_issue",
                side_effect=inject_after_first_blocker_observation(),
            ),
            contextlib.redirect_stdout(uninstall_output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket representation must be reconciled",
            ),
        ):
            MODULE.uninstall_overlay(
                self.home,
                "private",
                dry_run=True,
            )
        self.assertNotIn("would clean", uninstall_output.getvalue())
        self.assertTrue(ready_marker.is_file())
        self.assertTrue(malformed_ticket.is_file())


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
