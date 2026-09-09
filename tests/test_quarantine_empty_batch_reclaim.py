from __future__ import annotations

import contextlib
import errno
import gc
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_quarantine_empty_batch_reclaim",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class QuarantineEmptyBatchReclaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name) / "home"
        self.home.mkdir(mode=0o700)
        self.source_parent = self.home / "agents"
        self.source_parent.mkdir(mode=0o700)
        self.source = self.source_parent / "reviewer.toml"
        self.source.write_bytes(b'role = "reviewer"\n')
        self.source.chmod(0o600)
        metadata = self.source.stat()
        self.source_identity = (metadata.st_dev, metadata.st_ino)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _move_after_real_parent_policy_drift(self) -> None:
        source_parent_fd = MODULE._open_directory_beneath(
            self.home,
            self.source_parent,
        )
        real_quarantine_batch_root = MODULE._quarantine_batch_root

        def make_source_parent_group_writable(*args: object, **kwargs: object):
            allocation = real_quarantine_batch_root(*args, **kwargs)
            self.source_parent.chmod(0o770)
            return allocation

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_quarantine_batch_root",
                    side_effect=make_source_parent_group_writable,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "managed regular-file parent access policy mismatch",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    source_parent_fd,
                    self.source.name,
                    label="empty-batch-reclaim-test",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(source_parent_fd)

    def _assert_source_was_not_moved(self) -> None:
        self.assertEqual(self.source.read_bytes(), b'role = "reviewer"\n')
        metadata = self.source.stat()
        self.assertEqual((metadata.st_dev, metadata.st_ino), self.source_identity)

    def _assert_created_leaf_cleanup_failure(
        self,
        name: str,
        *,
        original_failure: str,
        cleanup_failure: str,
    ) -> None:
        if callable(getattr(MODULE.SyncError("probe"), "add_note", None)):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "private isolation failed",
            ) as raised:
                self._evacuate_created_leaf(name)
            original_error = raised.exception.__cause__
            self.assertIsInstance(original_error, MODULE.SyncError)
            assert original_error is not None
            self.assertIn(original_failure, str(original_error))
            notes = "\n".join(getattr(original_error, "__notes__", ()))
            self.assertIn("could not be safely reclaimed", notes)
            self.assertIn(cleanup_failure, notes)
        else:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "could not be safely reclaimed",
            ) as raised:
                self._evacuate_created_leaf(name)
            self.assertIn(original_failure, str(raised.exception))
            self.assertIn(cleanup_failure, str(raised.exception))
            cleanup_error = raised.exception.__cause__
            self.assertIsInstance(cleanup_error, MODULE.SyncError)
            assert cleanup_error is not None
            self.assertIn(cleanup_failure, str(cleanup_error))

    def _evacuate_created_leaf(self, name: str) -> None:
        target = self.source_parent / name
        payload = b'role = "reviewer"\n'
        target.write_bytes(payload)
        target.chmod(0o600)
        target_stat = target.stat()
        target_identity = (target_stat.st_dev, target_stat.st_ino)
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            MODULE._evacuate_created_regular_leaf_after_failure(
                self.home,
                target,
                parent_fd,
                MODULE._directory_identity(parent_fd),
                target_identity,
                payload,
            )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def _allocate_unowned_scaffold(
        self,
    ) -> tuple[
        Path,
        MODULE.EphemeralQuarantineBatchBinding,
    ]:
        allocation = MODULE._quarantine_batch_root(
            self.home,
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
        batch_root = allocation.batch_root
        binding = allocation.binding
        allocation.revoke_reclaim()
        allocation.close()
        return batch_root, binding

    def _resume_scaffold_cleanup(self, batch_root: Path) -> None:
        MODULE._cleanup_pending_cleanup_ticket_temps(self.home, limit=8)
        ticket = MODULE._read_pending_cleanup_ticket(
            self.home,
            MODULE._pending_cleanup_ticket_path(self.home, batch_root.name),
        )
        self.assertIsNotNone(ticket)
        assert ticket is not None
        self.assertEqual(ticket.version, 7)
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(batch_root.exists())
        index = MODULE._pending_cleanup_index_path(self.home)
        self.assertFalse(list(index.glob(f"{batch_root.name}*")))

    def _write_v7_ticket_temp(self) -> Path:
        batch_name = f"20260905T010101Z-{os.getpid()}-{os.getpid()}"
        temp_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_name,
        ).with_name(batch_name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            temp_path.parent,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        payload = MODULE._bounded_json_document(
            {
                "version": 7,
                "kind": "ephemeral-quarantine-scaffold",
                "batch": batch_name,
                "batch_root_identity": [101, 201],
                "quarantine_root_identity": [102, 202],
                "isolated_name": MODULE._pending_cleanup_isolated_batch_name(
                    batch_name
                ),
                "leaf": None,
                "metadata": {
                    "path": "metadata.json",
                    "file_identity": [103, 203],
                    "mode": 0o600,
                    "size": 0,
                    "sha256": MODULE.hashlib.sha256(b"").hexdigest(),
                },
            },
            max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
            overflow_error="test payload too large",
        )
        MODULE._write_exclusive_internal_file(self.home, temp_path, payload)
        return temp_path

    def _rewrite_v8_metadata_size(
        self,
        ticket: MODULE.PendingQuarantineAllocationTicket,
    ) -> None:
        assert ticket.snapshot.payload is not None
        payload = json.loads(ticket.path.read_bytes())
        payload["metadata"]["size"] += 1
        ticket.path.write_bytes(
            MODULE._bounded_json_document(
                payload,
                max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
                overflow_error="test allocation exceeds the size limit",
            )
        )
        ticket.path.chmod(0o600)

    def _crash_after_private_use_receipt(
        self,
    ) -> tuple[
        MODULE.PendingPrivateUseRetirementReceipt,
        Path,
    ]:
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_retire_pending_private_use_controls",
                    side_effect=SystemExit("injected post-receipt crash"),
                ),
                self.assertRaisesRegex(SystemExit, "post-receipt crash"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="private-use-receipt-crash",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        index = MODULE._pending_cleanup_index_path(self.home)
        receipt_paths = list(
            index.glob(f"*{MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX}")
        )
        self.assertEqual(len(receipt_paths), 1)
        receipt = MODULE._read_pending_private_use_retirement_receipt(
            self.home, receipt_paths[0]
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        private_path = receipt.batch_root / "leaf" / receipt.private_member_name
        self.assertTrue(private_path.is_file())
        self.assertFalse(self.source.exists())
        return receipt, private_path

    def _allocate_v5_ticket_representation(
        self,
        *,
        retained: bool,
    ) -> tuple[Path, MODULE.EphemeralQuarantineBatchBinding, Path]:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        binding = allocation.binding
        assert binding.cleanup_ticket is not None
        canonical = binding.cleanup_ticket.path
        temp = canonical.with_name(
            binding.batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        canonical.rename(temp)
        representation = temp
        if retained:
            retained_name = next(MODULE._retained_pending_cleanup_names(temp))
            representation = temp.with_name(retained_name)
            temp.rename(representation)
        allocation.revoke_reclaim()
        allocation.close()
        return binding.batch_root, binding, representation

    def _crash_before_private_use_receipt_publication(self) -> Path:
        real_rename = MODULE._rename_noreplace_at

        def crash_before_receipt_publication(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            if source_name.endswith(
                MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX
                + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
            ) and destination_name.endswith(
                MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX
            ):
                raise SystemExit("injected receipt publication crash")
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=crash_before_receipt_publication,
                ),
                self.assertRaisesRegex(SystemExit, "receipt publication crash"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="receipt-temp-crash",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        index = MODULE._pending_cleanup_index_path(self.home)
        return next(
            index.glob(
                "*"
                + MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX
                + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
            )
        )

    def _exercise_private_control_tombstone_crash(self, suffix: str) -> None:
        real_rename = MODULE._rename_noreplace_at

        def isolate_control_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name.endswith(suffix) and destination_name.startswith(
                MODULE.PENDING_CLEANUP_RETAINED_PREFIX + source_name + "-"
            ):
                raise SystemExit("injected control tombstone crash")

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=isolate_control_then_crash,
                ),
                self.assertRaisesRegex(SystemExit, "control tombstone crash"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="control-tombstone-crash",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        index = MODULE._pending_cleanup_index_path(self.home)
        receipt_path = next(
            index.glob(f"*{MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX}")
        )
        receipt = MODULE._read_pending_private_use_retirement_receipt(
            self.home, receipt_path
        )
        assert receipt is not None
        private_path = receipt.batch_root / "leaf" / receipt.private_member_name
        retained = list(
            index.glob(MODULE.PENDING_CLEANUP_RETAINED_PREFIX + "*" + suffix + "-*")
        )
        self.assertEqual(len(retained), 1)

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertFalse(retained[0].exists())
        self.assertFalse(receipt_path.exists())

    def _exercise_late_v8_representation_injection(
        self,
        *,
        retained: bool,
    ) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home, binding
        )
        allocation = binding.allocation_ticket
        assert allocation is not None
        metadata = batch_root / "metadata.json"
        expected_metadata = metadata.read_bytes()
        residue_path = allocation.path.with_name(allocation.path.name + ".extra")
        if retained:
            residue_path = residue_path.with_name(
                next(MODULE._retained_pending_cleanup_names(residue_path))
            )
        real_revalidate = MODULE._require_joined_quarantine_allocation_unchanged
        boundary_calls = 0

        def inject_representation(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
            expected: MODULE.PendingQuarantineAllocationTicket | None,
        ) -> None:
            nonlocal boundary_calls
            boundary_calls += 1
            if boundary_calls == 2:
                residue_path.write_bytes(b"foreign\n")
                residue_path.chmod(0o600)
            real_revalidate(home, ticket, expected)

        with (
            mock.patch.object(
                MODULE,
                "_require_joined_quarantine_allocation_unchanged",
                side_effect=inject_representation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "allocation representation must be reconciled",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        self.assertEqual(boundary_calls, 2)
        self.assertEqual(metadata.read_bytes(), expected_metadata)
        self.assertEqual(residue_path.read_bytes(), b"foreign\n")
        self.assertTrue(cleanup.path.exists())
        self.assertTrue(allocation.path.exists())

    def _exercise_metadata_cleanup_boundary_mutation(
        self,
        *,
        boundary_call: int,
        mutation: str,
    ) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        real_boundary = MODULE._require_pending_ephemeral_metadata_cleanup_boundary
        boundary_calls = 0
        evidence_batch_root = batch_root

        def mutate_boundary() -> None:
            nonlocal evidence_batch_root
            if mutation == "leaf":
                (evidence_batch_root / "leaf").mkdir(mode=0o700)
                return
            if mutation == "ticket":
                original_ticket_fd = os.open(ticket.path, os.O_RDONLY)
                try:
                    ticket.path.unlink()
                    ticket.path.write_bytes(b"{}\n")
                    ticket.path.chmod(0o600)
                finally:
                    os.close(original_ticket_fd)
                return
            if mutation == "batch-policy":
                evidence_batch_root.chmod(0o755)
                return
            if mutation == "root-identity":
                quarantine_root = evidence_batch_root.parent
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                evidence_batch_root = moved_root / batch_root.name
                return
            raise AssertionError(f"unsupported mutation: {mutation}")

        def inject_at_internal_boundary(*args: object, **kwargs: object) -> None:
            nonlocal boundary_calls
            boundary_calls += 1
            if boundary_calls == boundary_call:
                mutate_boundary()
            real_boundary(*args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_ephemeral_metadata_cleanup_boundary",
                side_effect=inject_at_internal_boundary,
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertEqual(boundary_calls, boundary_call)
        if boundary_call == 1:
            self.assertTrue((evidence_batch_root / "metadata.json").is_file())
        else:
            self.assertFalse((evidence_batch_root / "metadata.json").exists())
            retained = list(
                evidence_batch_root.glob(
                    MODULE.PENDING_CLEANUP_RETAINED_PREFIX + "metadata.json-*"
                )
            )
            self.assertEqual(len(retained), 1)
            self.assertTrue(retained[0].is_file())

    def test_parent_policy_precheck_failure_reclaims_empty_batch(self) -> None:
        for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
            self.source_parent.chmod(0o700)
            self._move_after_real_parent_policy_drift()
            self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

        self._assert_source_was_not_moved()

    def test_regular_move_rejects_root_replacement_before_private_rename(
        self,
    ) -> None:
        real_create_directory = MODULE._create_ephemeral_quarantine_leaf_at
        relocated_batch: Path | None = None

        def relocate_bound_batch_after_leaf_creation(
            allocation: MODULE.EphemeralQuarantineBatchAllocation,
        ) -> None:
            nonlocal relocated_batch
            real_create_directory(allocation)
            if relocated_batch is None:
                quarantine_root = (
                    MODULE._personal_sync_root(self.home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                )
                batch_root = next(quarantine_root.iterdir())
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                relocated_batch = quarantine_root / batch_root.name
                (moved_root / batch_root.name).rename(relocated_batch)

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_create_ephemeral_quarantine_leaf_at",
                    side_effect=relocate_bound_batch_after_leaf_creation,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "namespace changed before private rename",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="root-replacement",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self._assert_source_was_not_moved()
        self.assertIsNotNone(relocated_batch)
        assert relocated_batch is not None
        self.assertEqual(list((relocated_batch / "leaf").iterdir()), [])

    def test_created_move_rejects_root_replacement_before_private_rename(
        self,
    ) -> None:
        real_create_directory = MODULE._create_ephemeral_quarantine_leaf_at
        relocated_batch: Path | None = None

        def relocate_bound_batch_after_leaf_creation(
            allocation: MODULE.EphemeralQuarantineBatchAllocation,
        ) -> None:
            nonlocal relocated_batch
            real_create_directory(allocation)
            if relocated_batch is None:
                quarantine_root = (
                    MODULE._personal_sync_root(self.home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                )
                batch_root = next(quarantine_root.iterdir())
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                relocated_batch = quarantine_root / batch_root.name
                (moved_root / batch_root.name).rename(relocated_batch)

        with mock.patch.object(
            MODULE,
            "_create_ephemeral_quarantine_leaf_at",
            side_effect=relocate_bound_batch_after_leaf_creation,
        ):
            self._assert_created_leaf_cleanup_failure(
                "created-root-replacement.toml",
                original_failure=(
                    "ephemeral quarantine namespace changed before private rename"
                ),
                cleanup_failure="ephemeral quarantine root changed",
            )

        self.assertIsNotNone(relocated_batch)
        assert relocated_batch is not None
        self.assertEqual(list((relocated_batch / "leaf").iterdir()), [])
        aliases = tuple(self.source_parent.glob(".codex-created-leaf-*.evidence"))
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0].read_bytes(), b'role = "reviewer"\n')

    def test_private_boundary_rechecks_metadata_after_namespace_inventory(
        self,
    ) -> None:
        real_members = MODULE._directory_member_names
        leaf_inventory_calls = 0

        def mutate_after_final_namespace_inventory(
            directory_fd: int,
            *args: object,
            **kwargs: object,
        ) -> tuple[str, ...]:
            nonlocal leaf_inventory_calls
            names = real_members(directory_fd, *args, **kwargs)
            if names == ("leaf", "metadata.json"):
                leaf_inventory_calls += 1
                if leaf_inventory_calls == 2:
                    quarantine_root = (
                        MODULE._personal_sync_root(self.home)
                        / MODULE.QUARANTINE_RELATIVE_PATH
                    )
                    batch_root = next(quarantine_root.iterdir())
                    metadata = batch_root / "metadata.json"
                    metadata.write_bytes(b'{"replaced": true}\n')
                    metadata.chmod(0o600)
            return names

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_directory_member_names",
                    side_effect=mutate_after_final_namespace_inventory,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "ephemeral quarantine metadata changed",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="metadata-final-boundary",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self._assert_source_was_not_moved()

    def test_private_boundary_rechecks_leaf_policy_after_namespace_inventory(
        self,
    ) -> None:
        real_members = MODULE._directory_member_names
        leaf_inventory_calls = 0

        def relax_policy_after_final_namespace_inventory(
            directory_fd: int,
            *args: object,
            **kwargs: object,
        ) -> tuple[str, ...]:
            nonlocal leaf_inventory_calls
            names = real_members(directory_fd, *args, **kwargs)
            if names == ("leaf", "metadata.json"):
                leaf_inventory_calls += 1
                if leaf_inventory_calls == 2:
                    quarantine_root = (
                        MODULE._personal_sync_root(self.home)
                        / MODULE.QUARANTINE_RELATIVE_PATH
                    )
                    batch_root = next(quarantine_root.iterdir())
                    (batch_root / "leaf").chmod(0o755)
            return names

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_directory_member_names",
                    side_effect=relax_policy_after_final_namespace_inventory,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending cleanup access policy mismatch",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="policy-final-boundary",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self._assert_source_was_not_moved()

    def test_created_leaf_quarantine_leaf_creation_failure_does_not_leak_capacity(
        self,
    ) -> None:
        def fail_leaf_creation(
            allocation: MODULE.EphemeralQuarantineBatchAllocation,
            *args: object,
            **kwargs: object,
        ) -> None:
            del allocation, args, kwargs
            raise OSError("injected quarantine leaf creation failure")

        with mock.patch.object(
            MODULE,
            "_create_ephemeral_quarantine_leaf_at",
            side_effect=fail_leaf_creation,
        ):
            for attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "private quarantine setup failed",
                ):
                    self._evacuate_created_leaf(f"leaf-create-failure-{attempt}.toml")
                self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_created_leaf_pre_isolation_failure_does_not_leak_capacity(self) -> None:
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        failed_batches: set[Path] = set()
        post_ticket_checks: dict[Path, int] = {}

        def fail_each_leaf_once(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            batch_root = display_path.parent
            if (
                display_path.name == "leaf"
                and MODULE._pending_cleanup_ticket_path(
                    self.home,
                    batch_root.name,
                ).is_file()
            ):
                post_ticket_checks[batch_root] = (
                    post_ticket_checks.get(batch_root, 0) + 1
                )
                if (
                    post_ticket_checks[batch_root] == 2
                    and batch_root not in failed_batches
                ):
                    failed_batches.add(batch_root)
                    raise MODULE.SyncError("injected pre-isolation validation failure")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_require_pending_cleanup_fd_access_policy",
            side_effect=fail_each_leaf_once,
        ):
            for attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "private isolation failed",
                ):
                    self._evacuate_created_leaf(f"validation-failure-{attempt}.toml")
                self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

        self.assertEqual(
            len(failed_batches),
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1,
        )

    def test_created_leaf_replacement_with_content_is_retained(self) -> None:
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        replaced_batch: Path | None = None
        replacement_identity: tuple[int, int] | None = None
        post_ticket_checks = 0

        def replace_leaf_then_fail(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal post_ticket_checks, replaced_batch, replacement_identity
            if (
                display_path.name == "leaf"
                and MODULE._pending_cleanup_ticket_path(
                    self.home,
                    display_path.parent.name,
                ).is_file()
            ):
                post_ticket_checks += 1
            if (
                display_path.name == "leaf"
                and post_ticket_checks == 2
                and replaced_batch is None
            ):
                display_path.rmdir()
                display_path.mkdir(mode=0o700)
                foreign = display_path / "foreign-evidence"
                foreign.write_bytes(b"foreign\n")
                replacement_stat = display_path.stat()
                replacement_identity = (
                    replacement_stat.st_dev,
                    replacement_stat.st_ino,
                )
                replaced_batch = display_path.parent
                raise MODULE.SyncError("injected replaced-leaf validation failure")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_require_pending_cleanup_fd_access_policy",
            side_effect=replace_leaf_then_fail,
        ):
            self._assert_created_leaf_cleanup_failure(
                "replacement-race.toml",
                original_failure="injected replaced-leaf validation failure",
                cleanup_failure="ephemeral quarantine leaf changed",
            )

        self.assertIsNotNone(replaced_batch)
        self.assertIsNotNone(replacement_identity)
        assert replaced_batch is not None
        replacement = replaced_batch / "leaf"
        replacement_stat = replacement.stat()
        self.assertEqual(
            (replacement_stat.st_dev, replacement_stat.st_ino),
            replacement_identity,
        )
        self.assertEqual(
            (replacement / "foreign-evidence").read_bytes(),
            b"foreign\n",
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_created_leaf_metadata_replacement_after_allocation_is_retained(
        self,
    ) -> None:
        real_allocate = MODULE._quarantine_batch_root
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        allocated_batch: Path | None = None
        allocation_metadata_fd = -1
        original_metadata_identity: tuple[int, int] | None = None
        leaf_validation_failures = 0
        discard_calls = 0

        def replace_metadata_after_allocation(
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal allocated_batch, allocation_metadata_fd
            nonlocal original_metadata_identity
            allocation = real_allocate(*args, **kwargs)
            if not kwargs.get("retain_scaffold_binding"):
                return allocation
            assert isinstance(
                allocation,
                MODULE.EphemeralQuarantineBatchAllocation,
            )
            allocated_batch = allocation.batch_root
            allocation_metadata_fd = allocation.metadata_fd
            metadata_snapshot = allocation.binding.metadata
            original_metadata_identity = metadata_snapshot.file_identity
            metadata_path = allocated_batch / "metadata.json"
            original_stat = os.fstat(allocation_metadata_fd)
            self.assertEqual(
                (original_stat.st_dev, original_stat.st_ino),
                original_metadata_identity,
            )
            original_payload = os.pread(
                allocation_metadata_fd,
                original_stat.st_size,
                0,
            )
            metadata_path.unlink()
            metadata_path.write_bytes(b'{"foreign": true}\n')
            metadata_path.chmod(0o600)
            self.assertEqual(
                os.pread(allocation_metadata_fd, original_stat.st_size, 0),
                original_payload,
            )
            self.assertNotEqual(metadata_path.read_bytes(), original_payload)
            return allocation

        def fail_if_leaf_validation_is_reached(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal leaf_validation_failures
            if display_path.name == "leaf":
                leaf_validation_failures += 1
                raise MODULE.SyncError("injected pre-isolation validation failure")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        with (
            mock.patch.object(
                MODULE,
                "_quarantine_batch_root",
                side_effect=replace_metadata_after_allocation,
            ),
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_fd_access_policy",
                side_effect=fail_if_leaf_validation_is_reached,
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "private quarantine setup failed",
            ),
        ):
            self._evacuate_created_leaf("metadata-replacement.toml")

        self.assertEqual(leaf_validation_failures, 0)
        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(allocated_batch)
        self.assertIsNotNone(original_metadata_identity)
        with self.assertRaises(OSError):
            os.fstat(allocation_metadata_fd)
        assert allocated_batch is not None
        metadata_path = allocated_batch / "metadata.json"
        self.assertEqual(metadata_path.read_bytes(), b'{"foreign": true}\n')
        self.assertEqual(
            {member.name for member in allocated_batch.iterdir()},
            {"metadata.json"},
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_created_leaf_quarantine_root_replacement_is_retained(self) -> None:
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        real_bound_directory_matches = MODULE._bound_directory_matches
        original_root_identity: tuple[int, int] | None = None
        bound_root_identity: tuple[int, int] | None = None
        replaced_batch: Path | None = None
        moved_root: Path | None = None
        discard_calls = 0
        post_ticket_checks = 0

        def replace_quarantine_root_then_fail(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal original_root_identity, moved_root
            nonlocal post_ticket_checks
            nonlocal replaced_batch
            if (
                display_path.name == "leaf"
                and MODULE._pending_cleanup_ticket_path(
                    self.home,
                    display_path.parent.name,
                ).is_file()
            ):
                post_ticket_checks += 1
            if (
                display_path.name == "leaf"
                and post_ticket_checks == 2
                and replaced_batch is None
            ):
                batch_root = display_path.parent
                quarantine_root = batch_root.parent
                original_metadata = quarantine_root.stat()
                original_root_identity = (
                    original_metadata.st_dev,
                    original_metadata.st_ino,
                )
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                replaced_batch = quarantine_root / batch_root.name
                (moved_root / batch_root.name).rename(replaced_batch)
                raise MODULE.SyncError("injected quarantine root replacement")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal bound_root_identity, discard_calls
            discard_calls += 1
            bound_root_identity = binding.quarantine_root_identity
            real_discard(home, binding)

        def reject_replacement_root_binding(
            home: Path,
            directory: Path,
            directory_fd: int,
        ) -> bool:
            if replaced_batch is not None and directory == replaced_batch.parent:
                return False
            return real_bound_directory_matches(home, directory, directory_fd)

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_fd_access_policy",
                side_effect=replace_quarantine_root_then_fail,
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            mock.patch.object(
                MODULE,
                "_bound_directory_matches",
                side_effect=reject_replacement_root_binding,
            ),
        ):
            self._assert_created_leaf_cleanup_failure(
                "root-replacement.toml",
                original_failure="injected quarantine root replacement",
                cleanup_failure="ephemeral quarantine root changed",
            )

        self.assertEqual(discard_calls, 1)
        self.assertIsNotNone(original_root_identity)
        self.assertEqual(bound_root_identity, original_root_identity)
        self.assertIsNotNone(moved_root)
        assert moved_root is not None
        self.assertEqual(list(moved_root.iterdir()), [])
        self.assertIsNotNone(replaced_batch)
        assert replaced_batch is not None
        self.assertEqual(
            {member.name for member in replaced_batch.iterdir()},
            {"leaf", "metadata.json"},
        )
        self.assertEqual(list((replaced_batch / "leaf").iterdir()), [])
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_created_leaf_ambiguous_private_rename_never_reclaims_batch(
        self,
    ) -> None:
        real_rename = MODULE._rename_noreplace_at
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        moved_evidence: Path | None = None
        discard_calls = 0

        def rename_move_private_leaf_then_throw(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal moved_evidence
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name.startswith(".codex-created-leaf-"):
                moved_name = ".codex-moved-private-created-leaf.evidence"
                real_rename(
                    destination_parent_fd,
                    destination_name,
                    source_parent_fd,
                    moved_name,
                )
                moved_evidence = self.source_parent / moved_name
                raise SystemExit("injected post-rename signal")

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=rename_move_private_leaf_then_throw,
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "private isolation failed"),
        ):
            self._evacuate_created_leaf("ambiguous-rename.toml")

        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(moved_evidence)
        assert moved_evidence is not None
        self.assertEqual(moved_evidence.read_bytes(), b'role = "reviewer"\n')
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        batches = tuple(quarantine_root.iterdir())
        self.assertEqual(len(batches), 1)
        self.assertEqual(
            {member.name for member in batches[0].iterdir()},
            {"leaf", "metadata.json"},
        )
        self.assertEqual(list((batches[0] / "leaf").iterdir()), [])

    def test_foreign_leaf_sibling_prevents_empty_batch_reclaim(self) -> None:
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        mutated_batch: Path | None = None

        def add_foreign_sibling(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal mutated_batch
            mutated_batch = binding.batch_root
            foreign = binding.batch_root / "leaf" / "foreign-evidence"
            foreign.write_bytes(b"foreign\n")
            real_discard(home, binding)

        with mock.patch.object(
            MODULE,
            "_discard_empty_ephemeral_quarantine_batch",
            side_effect=add_foreign_sibling,
        ):
            self._move_after_real_parent_policy_drift()

        self.assertIsNotNone(mutated_batch)
        assert mutated_batch is not None
        self.assertEqual(
            (mutated_batch / "leaf" / "foreign-evidence").read_bytes(),
            b"foreign\n",
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        self._assert_source_was_not_moved()

    def test_leaf_replacement_prevents_empty_batch_reclaim(self) -> None:
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        real_bound_directory_matches = MODULE._bound_directory_matches
        mutated_batch: Path | None = None

        def replace_leaf_directory(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal mutated_batch
            mutated_batch = binding.batch_root
            leaf = binding.batch_root / "leaf"
            original_leaf_fd = os.open(
                leaf,
                MODULE._directory_open_flags(nofollow=True),
            )
            try:
                self.assertEqual(
                    MODULE._directory_identity(original_leaf_fd),
                    binding.leaf_identity,
                )
                leaf.rmdir()
                leaf.mkdir(mode=0o700)
                foreign = leaf / "foreign-evidence"
                foreign.write_bytes(b"foreign\n")
                self.assertEqual(
                    MODULE._directory_member_names(original_leaf_fd),
                    (),
                )
                real_discard(home, binding)
            finally:
                os.close(original_leaf_fd)

        def reject_replacement_leaf_binding(
            home: Path,
            directory: Path,
            directory_fd: int,
        ) -> bool:
            if mutated_batch is not None and directory == mutated_batch / "leaf":
                return False
            return real_bound_directory_matches(home, directory, directory_fd)

        with (
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=replace_leaf_directory,
            ),
            mock.patch.object(
                MODULE,
                "_bound_directory_matches",
                side_effect=reject_replacement_leaf_binding,
            ),
        ):
            self._move_after_real_parent_policy_drift()

        self.assertIsNotNone(mutated_batch)
        assert mutated_batch is not None
        self.assertEqual(
            (mutated_batch / "leaf" / "foreign-evidence").read_bytes(),
            b"foreign\n",
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        self._assert_source_was_not_moved()

    def test_regular_move_ambiguous_private_rename_never_reclaims_batch(
        self,
    ) -> None:
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        real_rename = MODULE._rename_noreplace_at
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        moved_evidence: Path | None = None
        discard_calls = 0

        def rename_move_private_leaf_then_throw(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal moved_evidence
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == self.source.name:
                moved_name = ".codex-moved-private-regular.evidence"
                real_rename(
                    destination_parent_fd,
                    destination_name,
                    source_parent_fd,
                    moved_name,
                )
                moved_evidence = self.source_parent / moved_name
                raise SystemExit("injected post-rename signal")

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=rename_move_private_leaf_then_throw,
                ),
                mock.patch.object(
                    MODULE,
                    "_discard_empty_ephemeral_quarantine_batch",
                    side_effect=observe_discard,
                ),
                self.assertRaisesRegex(SystemExit, "post-rename signal"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="ambiguous-regular",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(moved_evidence)
        assert moved_evidence is not None
        self.assertEqual(moved_evidence.read_bytes(), b'role = "reviewer"\n')
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_abandoned_allocation_resource_reclaims_and_closes_descriptors(
        self,
    ) -> None:
        descriptors: list[int] = []

        def interrupt_handoff() -> None:
            allocation = MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )
            assert isinstance(
                allocation,
                MODULE.EphemeralQuarantineBatchAllocation,
            )
            descriptors.extend(
                [
                    allocation.quarantine_fd,
                    allocation.batch_fd,
                    allocation.metadata_fd,
                ]
            )
            raise SystemExit("injected allocation handoff failure")

        with self.assertRaisesRegex(SystemExit, "handoff failure") as raised:
            interrupt_handoff()
        del raised
        gc.collect()

        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_allocation_close_detaches_fds_before_post_close_baseexception(
        self,
    ) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
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
        allocation.revoke_reclaim()
        metadata_fd = allocation.metadata_fd
        probe_fd = os.open(self.source, os.O_RDONLY)
        reused_fd = -1
        real_close = MODULE._close_fd_quietly

        class PostCloseAbort(BaseException):
            pass

        def close_then_reuse(file_descriptor: int) -> None:
            nonlocal reused_fd
            if file_descriptor == metadata_fd and reused_fd < 0:
                real_close(file_descriptor)
                os.dup2(probe_fd, file_descriptor)
                reused_fd = file_descriptor
                raise PostCloseAbort("injected failure after successful close")
            real_close(file_descriptor)

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_close_fd_quietly",
                    side_effect=close_then_reuse,
                ),
                self.assertRaisesRegex(PostCloseAbort, "successful close"),
            ):
                allocation.close()

            self.assertEqual(reused_fd, metadata_fd)
            self.assertEqual(
                (
                    allocation.leaf_fd,
                    allocation.metadata_fd,
                    allocation.batch_fd,
                    allocation.quarantine_fd,
                ),
                (-1, -1, -1, -1),
            )
            allocation.close()
            allocation.__del__()
            self.assertEqual(os.fstat(reused_fd).st_ino, self.source.stat().st_ino)
        finally:
            if reused_fd >= 0:
                os.close(reused_fd)
            os.close(probe_fd)

    def test_reclaim_keeps_allocation_descriptors_through_batch_removal(
        self,
    ) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        directory_descriptors = (
            allocation.quarantine_fd,
            allocation.batch_fd,
            allocation.leaf_fd,
        )
        metadata_fd = allocation.metadata_fd
        expected_identities = (
            allocation.binding.quarantine_root_identity,
            allocation.binding.batch_identity,
            allocation.binding.leaf_identity,
        )
        real_remove = MODULE._remove_cleanup_ready_batch
        observed_removal = False

        def observe_removal(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> bool:
            nonlocal observed_removal
            observed_removal = True
            self.assertEqual(
                tuple(MODULE._directory_identity(fd) for fd in directory_descriptors),
                expected_identities,
            )
            metadata = os.fstat(metadata_fd)
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                allocation.binding.metadata.file_identity,
            )
            return real_remove(home, ticket)

        with mock.patch.object(
            MODULE,
            "_remove_cleanup_ready_batch",
            side_effect=observe_removal,
        ):
            allocation.reclaim_empty()

        self.assertTrue(observed_removal)
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        for descriptor in (*directory_descriptors, metadata_fd):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_allocator_constructor_baseexception_reclaims_bound_scaffold(
        self,
    ) -> None:
        with (
            mock.patch.object(
                MODULE,
                "EphemeralQuarantineBatchAllocation",
                side_effect=SystemExit("injected constructor handoff failure"),
            ),
            self.assertRaisesRegex(SystemExit, "constructor handoff failure"),
        ):
            MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )

        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_allocator_exception_keeps_original_fds_through_removal(
        self,
    ) -> None:
        real_dup = os.dup
        original_batch_fd = -1
        original_metadata_fd = -1
        expected_metadata_identity: tuple[int, int] | None = None
        duplication_sources: dict[int, int] = {}
        real_remove = MODULE._remove_cleanup_ready_batch
        observed_removal = False

        def track_dup(file_descriptor: int) -> int:
            duplicate = real_dup(file_descriptor)
            duplication_sources[duplicate] = file_descriptor
            return duplicate

        def interrupt_constructor(
            *,
            batch_fd: int,
            metadata_fd: int,
            binding: MODULE.EphemeralQuarantineBatchBinding,
            **_kwargs: object,
        ) -> object:
            nonlocal original_batch_fd, original_metadata_fd
            nonlocal expected_metadata_identity
            original_batch_fd = duplication_sources[batch_fd]
            original_metadata_fd = duplication_sources[metadata_fd]
            expected_metadata_identity = binding.metadata.file_identity
            raise SystemExit("injected constructor handoff failure")

        def observe_removal(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> bool:
            nonlocal observed_removal
            observed_removal = True
            self.assertEqual(
                MODULE._directory_identity(original_batch_fd),
                ticket.batch_root_identity,
            )
            metadata = os.fstat(original_metadata_fd)
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                expected_metadata_identity,
            )
            return real_remove(home, ticket)

        with (
            mock.patch.object(os, "dup", side_effect=track_dup),
            mock.patch.object(
                MODULE,
                "EphemeralQuarantineBatchAllocation",
                side_effect=interrupt_constructor,
            ),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=observe_removal,
            ),
            self.assertRaisesRegex(
                SystemExit,
                "constructor handoff failure",
            ) as raised,
        ):
            MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )

        self.assertTrue(observed_removal)
        self.assertEqual(getattr(raised.exception, "__notes__", []), [])
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        with self.assertRaises(OSError):
            os.fstat(original_batch_fd)
        with self.assertRaises(OSError):
            os.fstat(original_metadata_fd)

    def test_allocator_exception_cleanup_retains_scaffold_after_root_drift(
        self,
    ) -> None:
        relocated_batch: Path | None = None

        def replace_root_then_interrupt(
            *,
            home: Path,
            quarantine_root: Path,
            quarantine_fd: int,
            batch_fd: int,
            metadata_fd: int,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> object:
            del home, quarantine_fd, batch_fd, metadata_fd
            nonlocal relocated_batch
            moved_root = quarantine_root.with_name(quarantine_root.name + ".original")
            quarantine_root.rename(moved_root)
            quarantine_root.mkdir(mode=0o700)
            relocated_batch = quarantine_root / binding.batch_root.name
            (moved_root / binding.batch_root.name).rename(relocated_batch)
            raise SystemExit("injected allocator root drift")

        with mock.patch.object(
            MODULE,
            "EphemeralQuarantineBatchAllocation",
            side_effect=replace_root_then_interrupt,
        ):
            if callable(getattr(SystemExit(), "add_note", None)):
                with self.assertRaisesRegex(
                    SystemExit,
                    "allocator root drift",
                ) as raised:
                    MODULE._quarantine_batch_root(
                        self.home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                notes = "\n".join(getattr(raised.exception, "__notes__", ()))
                self.assertIn("bound empty scaffold was retained", notes)
                self.assertIn("ephemeral quarantine root changed", notes)
            else:
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "bound empty scaffold was retained",
                ) as raised:
                    MODULE._quarantine_batch_root(
                        self.home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                self.assertIn("allocator root drift", str(raised.exception))
                self.assertIn(
                    "ephemeral quarantine root changed",
                    str(raised.exception),
                )
                cleanup_error = raised.exception.__cause__
                self.assertIsInstance(cleanup_error, MODULE.SyncError)
                assert cleanup_error is not None
                self.assertIn(
                    "ephemeral quarantine root changed",
                    str(cleanup_error),
                )

        self.assertIsNotNone(relocated_batch)
        assert relocated_batch is not None
        self.assertEqual(
            {member.name for member in relocated_batch.iterdir()},
            {"metadata.json"},
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_leaf_open_failure_after_replacement_retains_scaffold(self) -> None:
        real_allocate = MODULE._quarantine_batch_root
        real_open = os.open
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        allocation: MODULE.EphemeralQuarantineBatchAllocation | None = None
        failure_armed = False
        discard_calls = 0
        replacement_marker: Path | None = None

        def capture_allocation(*args: object, **kwargs: object) -> object:
            nonlocal allocation
            result = real_allocate(*args, **kwargs)
            if kwargs.get("retain_scaffold_binding"):
                assert isinstance(
                    result,
                    MODULE.EphemeralQuarantineBatchAllocation,
                )
                allocation = result
            return result

        def fail_first_leaf_open(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> int:
            nonlocal failure_armed, replacement_marker
            if path == "leaf" and failure_armed:
                failure_armed = False
                assert allocation is not None
                old_leaf_fd = real_open(path, *args, **kwargs)
                try:
                    self.assertEqual(
                        MODULE._directory_identity(old_leaf_fd),
                        allocation.binding.leaf_identity,
                    )
                    dir_fd = kwargs.get("dir_fd")
                    assert isinstance(dir_fd, int)
                    os.rmdir(path, dir_fd=dir_fd)
                    os.mkdir(path, mode=0o700, dir_fd=dir_fd)
                    replacement_marker = allocation.batch_root / "leaf" / "foreign"
                    replacement_marker.write_bytes(b"foreign\n")
                    self.assertEqual(
                        MODULE._directory_member_names(old_leaf_fd),
                        (),
                    )
                finally:
                    os.close(old_leaf_fd)
                raise OSError("injected post-mkdir leaf open failure")
            return real_open(path, *args, **kwargs)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        failure_armed = True
        with (
            mock.patch.object(
                MODULE,
                "_quarantine_batch_root",
                side_effect=capture_allocation,
            ),
            mock.patch.object(os, "open", side_effect=fail_first_leaf_open),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "post-mkdir leaf open failure",
            ),
        ):
            self._evacuate_created_leaf("leaf-open-replacement.toml")

        self.assertFalse(failure_armed)
        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(allocation)
        assert allocation is not None
        self.assertEqual(allocation.leaf_fd, -1)
        self.assertIsNotNone(replacement_marker)
        assert replacement_marker is not None
        self.assertEqual(replacement_marker.read_bytes(), b"foreign\n")
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        self.assertEqual(
            {entry.name for entry in allocation.batch_root.iterdir()},
            {"leaf", "metadata.json"},
        )

    def test_leaf_initialization_fsync_failure_retains_unbound_leaf(self) -> None:
        real_fsync = os.fsync
        failure_armed = False
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        discard_calls = 0

        def fail_first_bound_leaf_fsync(file_descriptor: int) -> None:
            nonlocal failure_armed
            try:
                names = MODULE._directory_member_names(
                    file_descriptor,
                    maximum_entries=3,
                )
            except (OSError, MODULE.SyncError):
                real_fsync(file_descriptor)
                return
            if names == ("leaf", "metadata.json") and failure_armed:
                failure_armed = False
                raise OSError("injected post-mkdir leaf fsync failure")
            real_fsync(file_descriptor)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        failure_armed = True
        with (
            mock.patch.object(os, "fsync", side_effect=fail_first_bound_leaf_fsync),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "post-mkdir leaf fsync failure",
            ),
        ):
            self._evacuate_created_leaf("leaf-fsync.toml")

        self.assertFalse(failure_armed)
        self.assertEqual(discard_calls, 0)
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_metadata_post_write_fsync_failure_reclaims_repeatedly(self) -> None:
        real_open = os.open
        real_fsync = os.fsync
        failure_armed = False
        metadata_fd_to_fail: int | None = None

        def track_metadata_create(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> int:
            nonlocal metadata_fd_to_fail
            file_descriptor = real_open(path, *args, **kwargs)
            flags = args[0] if args else 0
            if (
                failure_armed
                and isinstance(path, str)
                and path.endswith(MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX)
                and int(flags) & os.O_CREAT
            ):
                metadata_fd_to_fail = file_descriptor
            return file_descriptor

        def commit_then_fail_metadata_fsync(file_descriptor: int) -> None:
            nonlocal failure_armed, metadata_fd_to_fail
            metadata = os.fstat(file_descriptor)
            if (
                failure_armed
                and file_descriptor == metadata_fd_to_fail
                and metadata.st_size > 0
            ):
                failure_armed = False
                metadata_fd_to_fail = None
                real_fsync(file_descriptor)
                raise OSError("injected post-write metadata fsync failure")
            real_fsync(file_descriptor)

        with (
            mock.patch.object(os, "open", side_effect=track_metadata_create),
            mock.patch.object(
                os,
                "fsync",
                side_effect=commit_then_fail_metadata_fsync,
            ),
        ):
            for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
                failure_armed = True
                metadata_fd_to_fail = None
                with self.assertRaisesRegex(
                    OSError,
                    "post-write metadata fsync failure",
                ):
                    MODULE._quarantine_batch_root(
                        self.home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                self.assertFalse(failure_armed)
                self.assertIsNone(metadata_fd_to_fail)
                self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_metadata_stage_short_write_failure_reclaims_repeatedly(self) -> None:
        real_open = os.open
        real_write = os.write
        metadata_fd_to_fail: int | None = None
        wrote_prefix = False

        def track_metadata_stage(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> int:
            nonlocal metadata_fd_to_fail
            file_descriptor = real_open(path, *args, **kwargs)
            flags = args[0] if args else 0
            if (
                isinstance(path, str)
                and path.endswith(MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX)
                and int(flags) & os.O_CREAT
            ):
                metadata_fd_to_fail = file_descriptor
            return file_descriptor

        def short_write_then_fail(file_descriptor: int, payload: bytes) -> int:
            nonlocal metadata_fd_to_fail, wrote_prefix
            if file_descriptor != metadata_fd_to_fail:
                return real_write(file_descriptor, payload)
            if not wrote_prefix:
                wrote_prefix = True
                prefix_size = max(1, len(payload) // 2)
                return real_write(file_descriptor, payload[:prefix_size])
            metadata_fd_to_fail = None
            raise OSError("injected partial metadata stage write failure")

        with (
            mock.patch.object(os, "open", side_effect=track_metadata_stage),
            mock.patch.object(os, "write", side_effect=short_write_then_fail),
        ):
            for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
                metadata_fd_to_fail = None
                wrote_prefix = False
                with self.assertRaisesRegex(
                    OSError,
                    "partial metadata stage write failure",
                ) as raised:
                    MODULE._quarantine_batch_root(
                        self.home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                self.assertEqual(getattr(raised.exception, "__notes__", []), [])
                self.assertTrue(wrote_prefix)
                self.assertIsNone(metadata_fd_to_fail)
                self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
                index = MODULE._pending_cleanup_index_path(self.home)
                self.assertFalse(list(index.iterdir()))

    def test_partial_metadata_stage_survives_cleanup_crash_and_recovers(self) -> None:
        real_open = os.open
        real_write = os.write
        real_discard = MODULE._discard_incomplete_pending_cleanup_ticket
        metadata_fd_to_fail: int | None = None
        cleanup_crashed = False

        def track_metadata_stage(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> int:
            nonlocal metadata_fd_to_fail
            file_descriptor = real_open(path, *args, **kwargs)
            flags = args[0] if args else 0
            if (
                isinstance(path, str)
                and path.endswith(MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX)
                and int(flags) & os.O_CREAT
            ):
                metadata_fd_to_fail = file_descriptor
            return file_descriptor

        def short_write_then_fail(file_descriptor: int, payload: bytes) -> int:
            nonlocal metadata_fd_to_fail
            if file_descriptor != metadata_fd_to_fail:
                return real_write(file_descriptor, payload)
            real_write(file_descriptor, payload[: max(1, len(payload) // 2)])
            metadata_fd_to_fail = None
            raise OSError("injected partial metadata stage write failure")

        def crash_before_stage_cleanup(home: Path, path: Path, **kwargs: object) -> int:
            nonlocal cleanup_crashed
            if path.name.endswith(MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX):
                cleanup_crashed = True
                raise SystemExit("injected metadata stage cleanup crash")
            return real_discard(home, path, **kwargs)

        with (
            mock.patch.object(os, "open", side_effect=track_metadata_stage),
            mock.patch.object(os, "write", side_effect=short_write_then_fail),
            mock.patch.object(
                MODULE,
                "_discard_incomplete_pending_cleanup_ticket",
                side_effect=crash_before_stage_cleanup,
            ),
            self.assertRaisesRegex(SystemExit, "metadata stage cleanup crash"),
        ):
            MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )

        self.assertTrue(cleanup_crashed)
        index = MODULE._pending_cleanup_index_path(self.home)
        stages = list(index.glob(f"*{MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX}"))
        self.assertEqual(len(stages), 1)
        self.assertGreater(stages[0].stat().st_size, 0)
        self.assertFalse(
            list(
                MODULE._personal_sync_root(self.home)
                .joinpath(MODULE.QUARANTINE_RELATIVE_PATH)
                .iterdir()
            )
        )
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        self.assertFalse(list(index.iterdir()))

    def test_v8_with_metadata_stage_and_empty_batch_fails_closed(
        self,
    ) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        stage_fd = -1
        try:
            batch_name = f"20260905T005959Z-{os.getpid()}-{time.time_ns()}"
            payload = b'{"actions": []}\n'
            stage_path = MODULE._pending_quarantine_metadata_stage_path(
                self.home,
                batch_name,
            )
            index_fd = MODULE._open_or_create_directory_beneath(
                self.home,
                stage_path.parent,
                mode=0o700,
            )
            try:
                stage_fd = os.open(
                    stage_path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=index_fd,
                )
                os.write(stage_fd, payload)
                os.fsync(stage_fd)
                os.fsync(index_fd)
            finally:
                MODULE._close_fd_quietly(stage_fd)
                stage_fd = -1
                MODULE._close_fd_quietly(index_fd)
            allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                self.home,
                quarantine_root,
                quarantine_fd,
                batch_name,
                payload,
            )
            os.mkdir(batch_name, mode=0o700, dir_fd=quarantine_fd)
            os.fsync(quarantine_fd)
            batch_stat = os.stat(
                batch_name,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
            batch_identity = (batch_stat.st_dev, batch_stat.st_ino)
        finally:
            MODULE._close_fd_quietly(stage_fd)
            MODULE._close_fd_quietly(quarantine_fd)

        self.assertTrue(stage_path.is_file())
        self.assertTrue(allocation.path.is_file())
        self.assertTrue(allocation.batch_root.is_dir())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._pending_cleanup_ready_batch_is_observed(self.home)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )
        self.assertEqual(stage_path.read_bytes(), payload)
        current_batch = allocation.batch_root.stat()
        self.assertEqual(
            (current_batch.st_dev, current_batch.st_ino),
            batch_identity,
        )
        self.assertEqual(list(allocation.batch_root.iterdir()), [])
        self.assertTrue(allocation.path.is_file())

    def test_python39_allocator_cleanup_error_chains_both_failures(self) -> None:
        class LegacyBaseException(BaseException):
            add_note = None

        with (
            mock.patch.object(
                MODULE,
                "EphemeralQuarantineBatchAllocation",
                side_effect=LegacyBaseException("legacy allocation failure"),
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=MODULE.SyncError("bound cleanup failure"),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "bound cleanup failure.*legacy allocation failure",
            ),
        ):
            MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )

    def test_v8_ticket_only_recovery_retires_reservation(self) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        try:
            batch_name = f"20260905T000000Z-{os.getpid()}-{os.getpid()}"
            allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                self.home,
                quarantine_root,
                quarantine_fd,
                batch_name,
                b"{}\n",
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)

        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        dry_run_output = io.StringIO()
        with contextlib.redirect_stdout(dry_run_output):
            self.assertTrue(
                MODULE._preflight_pending_recovery(
                    self.home,
                    dry_run=True,
                )
            )
        self.assertIn("would clean", dry_run_output.getvalue())
        budget = MODULE.PendingCleanupActionBudget(4)
        self.assertEqual(
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=budget,
            ),
            1,
        )
        self.assertFalse(allocation.path.exists())
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_v8_retains_empty_canonical_and_isolated_batches(self) -> None:
        for initial_state in ("canonical", "isolated"):
            with self.subTest(initial_state=initial_state):
                case_home = self.home / initial_state
                case_home.mkdir(mode=0o700)
                quarantine_root = (
                    MODULE._personal_sync_root(case_home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                )
                quarantine_fd = MODULE._open_or_create_directory_beneath(
                    case_home,
                    quarantine_root,
                    mode=0o700,
                )
                try:
                    batch_name = (
                        f"20260905T01010{len(initial_state)}Z-"
                        f"{os.getpid()}-{time.time_ns()}"
                    )
                    allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                        case_home,
                        quarantine_root,
                        quarantine_fd,
                        batch_name,
                        b"{}\n",
                    )
                    directory_name = (
                        batch_name
                        if initial_state == "canonical"
                        else allocation.isolated_name
                    )
                    os.mkdir(directory_name, mode=0o700, dir_fd=quarantine_fd)
                    os.fsync(quarantine_fd)
                    directory_stat = os.stat(
                        directory_name,
                        dir_fd=quarantine_fd,
                        follow_symlinks=False,
                    )
                    directory_identity = (
                        directory_stat.st_dev,
                        directory_stat.st_ino,
                    )
                finally:
                    MODULE._close_fd_quietly(quarantine_fd)

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "present entity without exact cleanup authority",
                ):
                    MODULE._pending_cleanup_ready_batch_is_observed(case_home)
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "present entity without exact cleanup authority",
                ):
                    MODULE._cleanup_pending_quarantine_allocations(
                        case_home,
                        budget=MODULE.PendingCleanupActionBudget(4),
                    )
                retained = quarantine_root / directory_name
                retained_stat = retained.stat()
                self.assertEqual(
                    (retained_stat.st_dev, retained_stat.st_ino),
                    directory_identity,
                )
                self.assertEqual(list(retained.iterdir()), [])
                self.assertTrue(allocation.path.is_file())

    def test_v8_retains_exact_planned_leafless_metadata_scaffold(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.revoke_reclaim()
        allocation.close()
        assert allocation.binding.allocation_ticket is not None
        batch_stat = allocation.batch_root.stat()
        batch_identity = (batch_stat.st_dev, batch_stat.st_ino)
        metadata_path = allocation.batch_root / "metadata.json"
        metadata_stat = metadata_path.stat()
        metadata_identity = (metadata_stat.st_dev, metadata_stat.st_ino)
        metadata_payload = metadata_path.read_bytes()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._pending_cleanup_ready_batch_is_observed(self.home)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )
        current_batch = allocation.batch_root.stat()
        current_metadata = metadata_path.stat()
        self.assertEqual(
            (current_batch.st_dev, current_batch.st_ino),
            batch_identity,
        )
        self.assertEqual(
            (current_metadata.st_dev, current_metadata.st_ino),
            metadata_identity,
        )
        self.assertEqual(metadata_path.read_bytes(), metadata_payload)
        self.assertTrue(allocation.binding.allocation_ticket.path.is_file())

    def test_v5_and_v8_recover_exact_empty_leaf_after_crash(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        binding = allocation.binding
        assert binding.allocation_ticket is not None
        assert binding.cleanup_ticket is not None
        self.assertEqual(binding.cleanup_ticket.version, 5)
        batch_stat = allocation.batch_root.stat()
        leaf_stat = (allocation.batch_root / "leaf").stat()
        self.assertEqual(
            (batch_stat.st_dev, batch_stat.st_ino),
            binding.batch_identity,
        )
        self.assertEqual(
            (leaf_stat.st_dev, leaf_stat.st_ino),
            binding.leaf_identity,
        )
        allocation.revoke_reclaim()
        allocation.close()

        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(allocation.batch_root.exists())
        self.assertFalse(binding.cleanup_ticket.path.exists())
        self.assertFalse(binding.allocation_ticket.path.exists())

    def test_v5_and_v8_refuse_nonempty_leaf_recovery(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        binding = allocation.binding
        assert binding.allocation_ticket is not None
        assert binding.cleanup_ticket is not None
        allocation.revoke_reclaim()
        allocation.close()
        leaf = allocation.batch_root / "leaf"
        foreign = leaf / "foreign"
        foreign.write_bytes(b"foreign\n")
        leaf_stat = leaf.stat()
        leaf_identity = (leaf_stat.st_dev, leaf_stat.st_ino)

        with self.assertRaisesRegex(MODULE.SyncError, "leaf is not empty"):
            MODULE._cleanup_ready_pending_batches(self.home)

        current_leaf = leaf.stat()
        self.assertEqual(
            (current_leaf.st_dev, current_leaf.st_ino),
            leaf_identity,
        )
        self.assertEqual(foreign.read_bytes(), b"foreign\n")
        self.assertTrue(binding.cleanup_ticket.path.is_file())
        self.assertTrue(binding.allocation_ticket.path.is_file())

    def test_v5_and_v8_refuse_replaced_leaf_recovery(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        binding = allocation.binding
        assert binding.allocation_ticket is not None
        assert binding.cleanup_ticket is not None
        held_leaf_fd = os.dup(allocation.leaf_fd)
        real_bound_directory_matches = MODULE._bound_directory_matches
        allocation.revoke_reclaim()
        allocation.close()
        leaf = allocation.batch_root / "leaf"
        foreign = leaf / "foreign"

        def reject_replacement_leaf_binding(
            home: Path,
            directory: Path,
            directory_fd: int,
        ) -> bool:
            if directory == leaf:
                return False
            return real_bound_directory_matches(home, directory, directory_fd)

        try:
            leaf.rmdir()
            leaf.mkdir(mode=0o700)
            foreign.write_bytes(b"foreign\n")
            self.assertEqual(MODULE._directory_member_names(held_leaf_fd), ())
            with (
                mock.patch.object(
                    MODULE,
                    "_bound_directory_matches",
                    side_effect=reject_replacement_leaf_binding,
                ),
                self.assertRaisesRegex(MODULE.SyncError, "leaf changed"),
            ):
                MODULE._cleanup_ready_pending_batches(self.home)
        finally:
            os.close(held_leaf_fd)

        self.assertEqual(foreign.read_bytes(), b"foreign\n")
        self.assertTrue(binding.cleanup_ticket.path.is_file())
        self.assertTrue(binding.allocation_ticket.path.is_file())

    def test_v8_recovery_rejects_partial_planned_metadata(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.revoke_reclaim()
        allocation.close()
        metadata_path = allocation.batch_root / "metadata.json"
        original = metadata_path.read_bytes()
        metadata_path.write_bytes(original[: max(1, len(original) // 2)])
        metadata_path.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._pending_cleanup_ready_batch_is_observed(self.home)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )
        self.assertEqual(
            metadata_path.read_bytes(), original[: max(1, len(original) // 2)]
        )
        self.assertTrue(allocation.batch_root.exists())
        assert allocation.binding.allocation_ticket is not None
        self.assertTrue(allocation.binding.allocation_ticket.path.exists())

    def test_v8_retains_same_uid_replacement_created_after_reservation(
        self,
    ) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        try:
            batch_name = f"20260905T020202Z-{os.getpid()}-{time.time_ns()}"
            allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                self.home,
                quarantine_root,
                quarantine_fd,
                batch_name,
                b"{}\n",
            )
            os.mkdir(batch_name, mode=0o700, dir_fd=quarantine_fd)
            os.fsync(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        original = allocation.batch_root.with_name(batch_name + ".original")
        allocation.batch_root.rename(original)
        allocation.batch_root.mkdir(mode=0o700)
        foreign = allocation.batch_root / "foreign"
        foreign.write_bytes(b"foreign\n")
        replacement_stat = allocation.batch_root.stat()
        replacement_identity = (replacement_stat.st_dev, replacement_stat.st_ino)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )

        self.assertTrue(original.is_dir())
        current_stat = allocation.batch_root.stat()
        self.assertEqual(
            (current_stat.st_dev, current_stat.st_ino),
            replacement_identity,
        )
        self.assertEqual(foreign.read_bytes(), b"foreign\n")
        self.assertTrue(allocation.path.is_file())

    def test_v8_entity_with_matching_v7_is_observed_as_cleanup_ready(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            allocation.binding,
        )
        allocation.revoke_reclaim()
        allocation.close()

        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(ticket.path.exists())
        self.assertFalse(allocation.batch_root.exists())
        assert allocation.binding.allocation_ticket is not None
        self.assertFalse(allocation.binding.allocation_ticket.path.exists())

    def test_v8_entity_with_mismatched_v7_blocks_observation_and_cleanup(
        self,
    ) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            allocation.binding,
        )
        allocation.revoke_reclaim()
        allocation.close()
        payload = json.loads(cleanup.snapshot.payload)
        payload["metadata"]["size"] += 1
        cleanup.path.write_bytes(
            MODULE._bounded_json_document(
                payload,
                max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
                overflow_error="test ticket exceeds the size limit",
            )
        )
        cleanup.path.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join cleanup authority",
        ):
            MODULE._pending_cleanup_ready_batch_is_observed(self.home)

        dry_run_output = io.StringIO()
        with (
            contextlib.redirect_stdout(dry_run_output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "does not join cleanup authority",
            ),
        ):
            MODULE._preflight_pending_recovery(self.home, dry_run=True)
        self.assertNotIn("would clean", dry_run_output.getvalue())

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join cleanup authority",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )

        self.assertTrue(allocation.batch_root.is_dir())
        self.assertTrue(cleanup.path.is_file())
        assert allocation.binding.allocation_ticket is not None
        self.assertTrue(allocation.binding.allocation_ticket.path.is_file())

    def test_joined_v8_is_read_under_the_bound_cleanup_index_fd(self) -> None:
        _batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        allocation = binding.allocation_ticket
        assert allocation is not None
        real_read_allocation = MODULE._read_pending_quarantine_allocation_ticket
        captured_snapshots: list[MODULE.ManagedStateFileSnapshot | None] = []

        def record_captured_snapshot(*args: object, **kwargs: object):
            captured_snapshots.append(kwargs.get("_captured_snapshot"))
            return real_read_allocation(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_read_pending_quarantine_allocation_ticket",
            side_effect=record_captured_snapshot,
        ):
            joined = MODULE._read_joined_quarantine_allocation_for_cleanup(
                self.home,
                cleanup,
            )

        self.assertIsNotNone(joined)
        assert joined is not None
        self.assertTrue(
            MODULE._pending_quarantine_allocation_ticket_matches(joined, allocation)
        )
        self.assertEqual(len(captured_snapshots), 1)
        self.assertIsNotNone(captured_snapshots[0])

    def test_main_v7_cleanup_rejects_mismatched_v8_before_mutation(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        allocation = binding.allocation_ticket
        assert allocation is not None
        metadata = batch_root / "metadata.json"
        expected_metadata = metadata.read_bytes()
        self._rewrite_v8_metadata_size(allocation)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join cleanup authority",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        self.assertEqual(metadata.read_bytes(), expected_metadata)
        self.assertTrue(batch_root.is_dir())
        self.assertTrue(cleanup.path.is_file())
        self.assertTrue(allocation.path.is_file())

    def test_v7_cleanup_join_failure_closes_all_opened_index_fds(self) -> None:
        _batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        allocation = binding.allocation_ticket
        assert allocation is not None
        self._rewrite_v8_metadata_size(allocation)

        actions = (
            (
                "ticket deletion",
                lambda: MODULE._delete_pending_cleanup_ticket(self.home, cleanup),
            ),
            (
                "empty-proof deletion",
                lambda: MODULE._delete_pending_cleanup_empty_proof(
                    self.home,
                    cleanup,
                    cleanup.quarantine_root_identity,
                ),
            ),
        )
        for label, action in actions:
            with self.subTest(entry_point=label):
                real_open_directory = MODULE._open_directory_beneath
                opened_index_fds: list[int] = []

                def track_index_open(home: Path, path: Path) -> int:
                    fd = real_open_directory(home, path)
                    if path == cleanup.path.parent:
                        opened_index_fds.append(fd)
                    return fd

                with (
                    mock.patch.object(
                        MODULE,
                        "_open_directory_beneath",
                        side_effect=track_index_open,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "does not join cleanup authority",
                    ),
                ):
                    action()

                self.assertGreaterEqual(len(opened_index_fds), 2)
                for fd in opened_index_fds:
                    with self.assertRaises(OSError) as raised:
                        os.fstat(fd)
                    self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_main_v7_cleanup_revalidates_v8_before_metadata_mutation(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        allocation = binding.allocation_ticket
        assert allocation is not None
        metadata = batch_root / "metadata.json"
        expected_metadata = metadata.read_bytes()
        real_revalidate = MODULE._require_joined_quarantine_allocation_unchanged
        boundary_calls = 0

        def change_v8_at_metadata_boundary(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
            expected: MODULE.PendingQuarantineAllocationTicket | None,
        ) -> None:
            nonlocal boundary_calls
            boundary_calls += 1
            if boundary_calls == 2:
                self._rewrite_v8_metadata_size(allocation)
            real_revalidate(home, ticket, expected)

        with (
            mock.patch.object(
                MODULE,
                "_require_joined_quarantine_allocation_unchanged",
                side_effect=change_v8_at_metadata_boundary,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "does not join cleanup authority",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        self.assertEqual(boundary_calls, 2)
        self.assertEqual(metadata.read_bytes(), expected_metadata)
        self.assertTrue(batch_root.is_dir())
        self.assertTrue(cleanup.path.is_file())
        self.assertTrue(allocation.path.is_file())

    def test_v5_leaf_rmdir_retains_replacement_during_private_isolation(
        self,
    ) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        binding = allocation.binding
        cleanup = binding.cleanup_ticket
        allocation_ticket = binding.allocation_ticket
        assert cleanup is not None
        assert allocation_ticket is not None
        assert binding.leaf_identity is not None
        leaf = binding.batch_root / "leaf"
        retained_leaf = binding.batch_root / "authorized-leaf"
        allocation.revoke_reclaim()
        allocation.close()
        real_rename = MODULE._rename_noreplace_at
        replaced = False
        replacement_identity: tuple[int, int] | None = None

        def replace_leaf_before_atomic_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal replaced, replacement_identity
            if (
                not replaced
                and source_name == "leaf"
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
            ):
                leaf.rename(retained_leaf)
                leaf.mkdir(mode=0o700)
                replacement = leaf.stat()
                replacement_identity = (replacement.st_dev, replacement.st_ino)
                replaced = True
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=replace_leaf_before_atomic_isolation,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "leaf changed before removal"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, cleanup)

        self.assertTrue(replaced)
        self.assertIsNotNone(replacement_identity)
        retained_evidence = list(
            binding.batch_root.glob(MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX + "*")
        )
        self.assertEqual(len(retained_evidence), 1)
        self.assertEqual(
            (
                retained_evidence[0].stat().st_dev,
                retained_evidence[0].stat().st_ino,
            ),
            replacement_identity,
        )
        self.assertEqual(
            (retained_leaf.stat().st_dev, retained_leaf.stat().st_ino),
            binding.leaf_identity,
        )
        self.assertFalse(leaf.exists())
        self.assertEqual(tuple(retained_evidence[0].iterdir()), ())
        self.assertTrue(cleanup.path.is_file())
        self.assertTrue(allocation_ticket.path.is_file())

    def test_v7_batch_rmdir_retains_replacement_during_private_isolation(
        self,
    ) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        allocation_ticket = binding.allocation_ticket
        assert allocation_ticket is not None
        isolated = batch_root.with_name(cleanup.isolated_name or "")
        retained_batch = isolated.with_name(isolated.name + ".authorized")
        proof = MODULE._pending_cleanup_empty_proof_path(self.home, batch_root.name)
        real_rename = MODULE._rename_noreplace_at
        replaced = False
        replacement_identity: tuple[int, int] | None = None

        def replace_batch_before_atomic_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal replaced, replacement_identity
            if (
                not replaced
                and source_name == isolated.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
            ):
                isolated.rename(retained_batch)
                isolated.mkdir(mode=0o700)
                replacement = isolated.stat()
                replacement_identity = (replacement.st_dev, replacement.st_ino)
                replaced = True
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=replace_batch_before_atomic_isolation,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "batch changed before removal"),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, cleanup)

        self.assertTrue(replaced)
        self.assertIsNotNone(replacement_identity)
        retained_evidence = list(
            batch_root.parent.glob(MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX + "*")
        )
        self.assertEqual(len(retained_evidence), 1)
        self.assertEqual(
            (
                retained_evidence[0].stat().st_dev,
                retained_evidence[0].stat().st_ino,
            ),
            replacement_identity,
        )
        self.assertEqual(
            (retained_batch.stat().st_dev, retained_batch.stat().st_ino),
            binding.batch_identity,
        )
        self.assertFalse(isolated.exists())
        self.assertEqual(tuple(retained_evidence[0].iterdir()), ())
        self.assertTrue(proof.is_file())
        self.assertTrue(cleanup.path.is_file())
        self.assertTrue(allocation_ticket.path.is_file())

    def test_v5_leaf_rmdir_rechecks_parent_policy_after_mutation_revalidation(
        self,
    ) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        binding = allocation.binding
        cleanup = binding.cleanup_ticket
        allocation_ticket = binding.allocation_ticket
        assert cleanup is not None
        assert allocation_ticket is not None
        assert binding.leaf_identity is not None
        leaf = binding.batch_root / "leaf"
        allocation.revoke_reclaim()
        allocation.close()
        real_revalidate = MODULE._require_joined_quarantine_allocation_unchanged
        boundary_calls = 0

        def relax_parent_after_revalidation(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
            expected: MODULE.PendingQuarantineAllocationTicket | None,
        ) -> None:
            nonlocal boundary_calls
            real_revalidate(home, ticket, expected)
            boundary_calls += 1
            if boundary_calls == 2:
                binding.batch_root.chmod(0o770)

        with (
            mock.patch.object(
                MODULE,
                "_require_joined_quarantine_allocation_unchanged",
                side_effect=relax_parent_after_revalidation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "access policy mismatch",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, cleanup)

        self.assertEqual(boundary_calls, 2)
        self.assertEqual(
            (leaf.stat().st_dev, leaf.stat().st_ino),
            binding.leaf_identity,
        )
        self.assertFalse(
            list(
                binding.batch_root.glob(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX + "*"
                )
            )
        )
        self.assertTrue(cleanup.path.is_file())
        self.assertTrue(allocation_ticket.path.is_file())

    def test_directory_rmdir_rechecks_parent_policy_at_final_pathname_boundary(
        self,
    ) -> None:
        for scenario in ("v5-leaf", "v5-batch", "v7-batch"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                case_home = Path(root) / "home"
                case_home.mkdir(mode=0o700)
                allocation = MODULE._quarantine_batch_root(
                    case_home,
                    [],
                    retain_binding=True,
                    retain_scaffold_binding=True,
                )
                assert isinstance(
                    allocation,
                    MODULE.EphemeralQuarantineBatchAllocation,
                )
                if scenario.startswith("v5"):
                    allocation.create_leaf()
                    binding = allocation.binding
                    cleanup = binding.cleanup_ticket
                    assert cleanup is not None
                else:
                    binding = allocation.binding
                    cleanup = None
                allocation.revoke_reclaim()
                allocation.close()
                if cleanup is None:
                    cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                        case_home,
                        binding,
                    )
                allocation_ticket = binding.allocation_ticket
                assert allocation_ticket is not None

                if scenario == "v5-leaf":
                    target_parent = binding.batch_root
                    expected_identity = binding.leaf_identity
                else:
                    target_parent = binding.batch_root.parent
                    expected_identity = binding.batch_identity
                assert expected_identity is not None
                real_require_access = MODULE._require_pending_cleanup_fd_access_policy
                replacement_identity: tuple[int, int] | None = None
                authorized_member: Path | None = None
                replacement_member: Path | None = None
                private_parent_policy_checks = 0

                def relax_policy_and_replace_private_member(
                    directory_fd: int,
                    display_path: Path,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    nonlocal replacement_identity
                    nonlocal authorized_member, replacement_member
                    nonlocal private_parent_policy_checks
                    active = tuple(
                        target_parent.glob(
                            MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX + "*"
                        )
                    )
                    if display_path == target_parent and len(active) == 1:
                        private_parent_policy_checks += 1
                    if (
                        replacement_identity is None
                        and display_path == target_parent
                        and len(active) == 1
                        and private_parent_policy_checks == 2
                    ):
                        target_parent.chmod(0o770)
                        replacement_member = active[0]
                        authorized_member = active[0].with_name(
                            active[0].name + ".authorized"
                        )
                        active[0].rename(authorized_member)
                        active[0].mkdir(mode=0o700)
                        replacement = active[0].stat()
                        replacement_identity = (
                            replacement.st_dev,
                            replacement.st_ino,
                        )
                    return real_require_access(
                        directory_fd,
                        display_path,
                        *args,
                        **kwargs,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_require_pending_cleanup_fd_access_policy",
                        side_effect=relax_policy_and_replace_private_member,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "access policy mismatch",
                    ),
                ):
                    MODULE._remove_cleanup_ready_batch(case_home, cleanup)

                self.assertEqual(private_parent_policy_checks, 2)
                self.assertIsNotNone(replacement_identity)
                self.assertIsNotNone(authorized_member)
                self.assertIsNotNone(replacement_member)
                assert replacement_identity is not None
                assert authorized_member is not None
                assert replacement_member is not None
                self.assertEqual(
                    (
                        authorized_member.stat().st_dev,
                        authorized_member.stat().st_ino,
                    ),
                    expected_identity,
                )
                self.assertEqual(
                    (
                        replacement_member.stat().st_dev,
                        replacement_member.stat().st_ino,
                    ),
                    replacement_identity,
                )
                self.assertTrue(cleanup.path.is_file())
                self.assertTrue(allocation_ticket.path.is_file())

    def test_v5_cleanup_recovers_final_private_leaf_isolation(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        binding = allocation.binding
        cleanup = binding.cleanup_ticket
        assert cleanup is not None
        allocation.revoke_reclaim()
        allocation.close()
        real_rename = MODULE._rename_noreplace_at
        private_path: Path | None = None

        def isolate_private_leaf_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal private_path
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == "leaf" and destination_name.startswith(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
            ):
                private_path = binding.batch_root / destination_name
                raise SystemExit("injected final private leaf isolation crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=isolate_private_leaf_then_crash,
            ),
            self.assertRaisesRegex(
                SystemExit,
                "final private leaf isolation crash",
            ),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, cleanup)

        self.assertIsNotNone(private_path)
        assert private_path is not None
        self.assertTrue(private_path.is_dir())
        self.assertFalse((binding.batch_root / "leaf").exists())
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, cleanup))
        self.assertFalse(private_path.exists())
        self.assertFalse(binding.batch_root.exists())

    def test_main_v7_cleanup_rejects_late_direct_v8_descendant(self) -> None:
        self._exercise_late_v8_representation_injection(retained=False)

    def test_main_v7_cleanup_rejects_late_retained_v8_descendant(self) -> None:
        self._exercise_late_v8_representation_injection(retained=True)

    def test_absent_v8_with_matching_v7_is_delegated(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        allocation = binding.allocation_ticket
        assert allocation is not None
        (batch_root / "metadata.json").unlink()
        batch_root.rmdir()

        self.assertEqual(
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            ),
            0,
        )
        self.assertTrue(cleanup.path.is_file())
        self.assertTrue(allocation.path.is_file())

    def test_absent_v8_with_conflicting_v7_fails_closed(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        cleanup = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        allocation = binding.allocation_ticket
        assert allocation is not None
        (batch_root / "metadata.json").unlink()
        batch_root.rmdir()
        self._rewrite_v8_metadata_size(allocation)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join cleanup authority",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )

        self.assertTrue(cleanup.path.is_file())
        self.assertTrue(allocation.path.is_file())

    def test_v8_present_entity_never_enters_v8_only_mutation(self) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        try:
            batch_name = f"20260905T030301Z-{os.getpid()}-{time.time_ns()}"
            allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                self.home,
                quarantine_root,
                quarantine_fd,
                batch_name,
                b"{}\n",
            )
            os.mkdir(batch_name, mode=0o700, dir_fd=quarantine_fd)
            os.fsync(quarantine_fd)
        finally:
            MODULE._close_fd_quietly(quarantine_fd)

        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_quarantine_allocation_ticket",
            ) as delete_allocation,
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
            ) as discard_batch,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "present entity without exact cleanup authority",
            ),
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )

        delete_allocation.assert_not_called()
        discard_batch.assert_not_called()
        self.assertTrue(allocation.batch_root.is_dir())
        self.assertTrue(allocation.path.is_file())

    def test_absent_v8_rechecks_control_residue_before_direct_retirement(
        self,
    ) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        try:
            batch_name = f"20260905T030305Z-{os.getpid()}-{time.time_ns()}"
            allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                self.home,
                quarantine_root,
                quarantine_fd,
                batch_name,
                b"{}\n",
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)
        residue_path = allocation.path.with_name(
            batch_name + MODULE.PENDING_QUARANTINE_METADATA_STAGE_SUFFIX
        )
        real_require_absent = (
            MODULE._require_pending_quarantine_allocation_cleanup_controls_absent
        )
        injected = False

        def inject_metadata_stage(
            home: Path,
            index_root: Path,
            index_fd: int,
            ticket: MODULE.PendingQuarantineAllocationTicket,
            *,
            allowed_allocation_name: str,
        ) -> None:
            nonlocal injected
            if not injected:
                injected = True
                MODULE._write_exclusive_internal_file(
                    self.home,
                    residue_path,
                    b"{}\n",
                )
            real_require_absent(
                home,
                index_root,
                index_fd,
                ticket,
                allowed_allocation_name=allowed_allocation_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_quarantine_allocation_cleanup_controls_absent",
                side_effect=inject_metadata_stage,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "cleanup control remained"),
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )

        self.assertTrue(injected)
        self.assertTrue(residue_path.is_file())
        self.assertTrue(allocation.path.is_file())

    def test_v8_retained_and_temp_controls_fail_closed_in_read_only_paths(
        self,
    ) -> None:
        for representation in ("retained", "temp", "retained-temp"):
            with self.subTest(representation=representation):
                case_home = self.home / representation
                case_home.mkdir(mode=0o700)
                quarantine_root = (
                    MODULE._personal_sync_root(case_home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                )
                quarantine_fd = MODULE._open_or_create_directory_beneath(
                    case_home,
                    quarantine_root,
                    mode=0o700,
                )
                try:
                    batch_name = f"20260905T040404Z-{os.getpid()}-{time.time_ns()}"
                    allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                        case_home,
                        quarantine_root,
                        quarantine_fd,
                        batch_name,
                        b"{}\n",
                    )
                finally:
                    MODULE._close_fd_quietly(quarantine_fd)
                if representation == "retained":
                    control_path = allocation.path.with_name(
                        next(MODULE._retained_pending_cleanup_names(allocation.path))
                    )
                    allocation.path.rename(control_path)
                else:
                    temp_path = allocation.path.with_name(
                        batch_name + MODULE.PENDING_QUARANTINE_ALLOCATION_TEMP_SUFFIX
                    )
                    allocation.path.rename(temp_path)
                    control_path = temp_path
                    if representation == "retained-temp":
                        control_path = temp_path.with_name(
                            next(MODULE._retained_pending_cleanup_names(temp_path))
                        )
                        temp_path.rename(control_path)

                for observe in (
                    lambda: MODULE._pending_cleanup_ready_batch_is_observed(case_home),
                    lambda: MODULE.status(case_home),
                ):
                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "requires mutation recovery",
                    ):
                        observe()
                dry_run_output = io.StringIO()
                with (
                    contextlib.redirect_stdout(dry_run_output),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "requires mutation recovery",
                    ),
                ):
                    MODULE._preflight_pending_recovery(case_home, dry_run=True)
                self.assertNotIn("would clean", dry_run_output.getvalue())
                self.assertTrue(control_path.is_file())

                self.assertTrue(
                    MODULE._preflight_pending_recovery(case_home, dry_run=False)
                )
                self.assertFalse(control_path.exists())
                self.assertFalse(allocation.path.exists())
                self.assertEqual(MODULE._quarantine_batch_count(case_home), 0)

    def test_private_move_retires_v5_before_v8_after_double_verification(
        self,
    ) -> None:
        real_delete_cleanup = MODULE._delete_pending_cleanup_ticket
        real_delete_allocation = MODULE._delete_pending_quarantine_allocation_ticket
        retirement_order: list[str] = []

        def delete_cleanup(*args: object, **kwargs: object) -> None:
            retirement_order.append("v5")
            real_delete_cleanup(*args, **kwargs)

        def delete_allocation(*args: object, **kwargs: object) -> None:
            retirement_order.append("v8")
            real_delete_allocation(*args, **kwargs)

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_delete_pending_cleanup_ticket",
                    side_effect=delete_cleanup,
                ),
                mock.patch.object(
                    MODULE,
                    "_delete_pending_quarantine_allocation_ticket",
                    side_effect=delete_allocation,
                ),
            ):
                destination, moved, binding = (
                    MODULE._move_regular_leaf_to_unique_quarantine(
                        self.home,
                        self.source_parent,
                        parent_fd,
                        self.source.name,
                        label="v8-retirement",
                        expected_identity=self.source_identity,
                        retain_batch_binding=True,
                    )
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertFalse(self.source.exists())
        self.assertEqual(moved.file_identity, self.source_identity)
        self.assertTrue(destination.is_file())
        self.assertEqual(retirement_order, ["v5", "v8"])
        assert binding.cleanup_ticket is not None
        assert binding.allocation_ticket is not None
        self.assertFalse(binding.cleanup_ticket.path.exists())
        self.assertFalse(binding.allocation_ticket.path.exists())

    def test_private_use_receipt_recovers_with_v5_and_v8_present(self) -> None:
        receipt, private_path = self._crash_after_private_use_receipt()
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertFalse(receipt.path.exists())
        self.assertFalse(receipt.path.with_name(receipt.cleanup_control.name).exists())
        self.assertFalse(
            receipt.path.with_name(receipt.allocation_control.name).exists()
        )

    def test_private_use_receipt_rechecks_private_member_after_control_scan(
        self,
    ) -> None:
        receipt, private_path = self._crash_after_private_use_receipt()
        private_before = private_path.stat()
        real_control_state = MODULE._pending_private_use_retirement_control_state
        control_scans = 0

        def mutate_private_member_after_control_scan(
            *args: object,
            **kwargs: object,
        ) -> tuple[
            MODULE.PendingBatchCleanupTicket | None,
            MODULE.PendingQuarantineAllocationTicket | None,
        ]:
            nonlocal control_scans
            controls = real_control_state(*args, **kwargs)
            control_scans += 1
            if control_scans == 1:
                private_path.write_bytes(b"foreign\n")
                private_path.chmod(0o600)
            return controls

        with (
            mock.patch.object(
                MODULE,
                "_pending_private_use_retirement_control_state",
                side_effect=mutate_private_member_after_control_scan,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "private-use retirement private member changed",
            ),
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        private_after = private_path.stat()
        self.assertEqual(
            (private_after.st_dev, private_after.st_ino),
            (private_before.st_dev, private_before.st_ino),
        )
        self.assertEqual(private_path.read_bytes(), b"foreign\n")
        self.assertTrue(receipt.path.exists())
        self.assertTrue(receipt.path.with_name(receipt.cleanup_control.name).exists())
        self.assertTrue(
            receipt.path.with_name(receipt.allocation_control.name).exists()
        )

    def test_private_use_receipt_recovers_after_v5_retirement_crash(self) -> None:
        real_delete = MODULE._delete_pending_cleanup_ticket

        def delete_v5_then_crash(*args: object, **kwargs: object) -> None:
            real_delete(*args, **kwargs)
            raise SystemExit("injected post-v5 crash")

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_delete_pending_cleanup_ticket",
                    side_effect=delete_v5_then_crash,
                ),
                self.assertRaisesRegex(SystemExit, "post-v5 crash"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="post-v5-retirement-crash",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        index = MODULE._pending_cleanup_index_path(self.home)
        receipt_path = next(
            index.glob(f"*{MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX}")
        )
        receipt = MODULE._read_pending_private_use_retirement_receipt(
            self.home, receipt_path
        )
        assert receipt is not None
        private_path = receipt.batch_root / "leaf" / receipt.private_member_name
        self.assertFalse((index / receipt.cleanup_control.name).exists())
        self.assertTrue((index / receipt.allocation_control.name).is_file())

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertFalse(receipt_path.exists())
        self.assertFalse((index / receipt.allocation_control.name).exists())

    def test_private_use_receipt_recovers_after_v8_retirement_crash(self) -> None:
        real_delete = MODULE._delete_pending_quarantine_allocation_ticket

        def delete_v8_then_crash(*args: object, **kwargs: object) -> None:
            real_delete(*args, **kwargs)
            raise SystemExit("injected post-v8 crash")

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_delete_pending_quarantine_allocation_ticket",
                    side_effect=delete_v8_then_crash,
                ),
                self.assertRaisesRegex(SystemExit, "post-v8 crash"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="post-v8-retirement-crash",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        index = MODULE._pending_cleanup_index_path(self.home)
        receipt_path = next(
            index.glob(f"*{MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX}")
        )
        receipt = MODULE._read_pending_private_use_retirement_receipt(
            self.home, receipt_path
        )
        assert receipt is not None
        private_path = receipt.batch_root / "leaf" / receipt.private_member_name
        self.assertFalse((index / receipt.cleanup_control.name).exists())
        self.assertFalse((index / receipt.allocation_control.name).exists())

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertFalse(receipt_path.exists())

    def test_private_use_receipt_recovers_v5_tombstone_crash(self) -> None:
        self._exercise_private_control_tombstone_crash(
            MODULE.PENDING_CLEANUP_TICKET_SUFFIX
        )

    def test_private_use_receipt_recovers_v8_tombstone_crash(self) -> None:
        self._exercise_private_control_tombstone_crash(
            MODULE.PENDING_QUARANTINE_ALLOCATION_SUFFIX
        )

    def test_private_use_receipt_temp_is_promoted_and_recovered(self) -> None:
        temp_path = self._crash_before_private_use_receipt_publication()
        batch_name = MODULE._pending_private_use_retirement_temp_batch_name(
            temp_path.name
        )
        assert batch_name is not None

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertFalse(temp_path.exists())
        receipt_path = MODULE._pending_private_use_retirement_path(
            self.home, batch_name
        )
        self.assertFalse(receipt_path.exists())
        batch_root = (
            MODULE._personal_sync_root(self.home)
            / MODULE.QUARANTINE_RELATIVE_PATH
            / batch_name
        )
        private_members = list((batch_root / "leaf").iterdir())
        self.assertEqual(len(private_members), 1)
        self.assertEqual(private_members[0].read_bytes(), b'role = "reviewer"\n')

    def test_private_use_retained_receipt_temp_is_restored_and_recovered(
        self,
    ) -> None:
        temp_path = self._crash_before_private_use_receipt_publication()
        batch_name = MODULE._pending_private_use_retirement_temp_batch_name(
            temp_path.name
        )
        assert batch_name is not None
        retained_name = next(MODULE._retained_pending_cleanup_names(temp_path))
        retained_path = temp_path.with_name(retained_name)
        temp_path.rename(retained_path)

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertFalse(temp_path.exists())
        self.assertFalse(retained_path.exists())
        self.assertFalse(
            MODULE._pending_private_use_retirement_path(self.home, batch_name).exists()
        )
        batch_root = (
            MODULE._personal_sync_root(self.home)
            / MODULE.QUARANTINE_RELATIVE_PATH
            / batch_name
        )
        private_members = list((batch_root / "leaf").iterdir())
        self.assertEqual(len(private_members), 1)
        self.assertEqual(private_members[0].read_bytes(), b'role = "reviewer"\n')

    def test_private_use_retained_receipt_is_restored_and_recovered(self) -> None:
        receipt, private_path = self._crash_after_private_use_receipt()
        retained_name = next(MODULE._retained_pending_cleanup_names(receipt.path))
        retained_path = receipt.path.with_name(retained_name)
        receipt.path.rename(retained_path)

        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertFalse(retained_path.exists())
        self.assertFalse(receipt.path.exists())

    def test_private_use_receipt_tombstone_crash_recovers(self) -> None:
        real_rename = MODULE._rename_noreplace_at

        def isolate_receipt_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name.endswith(
                MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX
            ) and destination_name.startswith(
                MODULE.PENDING_CLEANUP_RETAINED_PREFIX + source_name + "-"
            ):
                raise SystemExit("injected receipt tombstone crash")

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=isolate_receipt_then_crash,
                ),
                self.assertRaisesRegex(SystemExit, "receipt tombstone crash"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="receipt-tombstone-crash",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        index = MODULE._pending_cleanup_index_path(self.home)
        retained = list(
            index.glob(
                MODULE.PENDING_CLEANUP_RETAINED_PREFIX
                + "*"
                + MODULE.PENDING_PRIVATE_USE_RETIREMENT_SUFFIX
                + "-*"
            )
        )
        self.assertEqual(len(retained), 1)
        batch_name = MODULE._pending_cleanup_retained_control_name(retained[0].name)[1]
        batch_root = (
            MODULE._personal_sync_root(self.home)
            / MODULE.QUARANTINE_RELATIVE_PATH
            / batch_name
        )
        private_path = next((batch_root / "leaf").iterdir())

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertFalse(retained[0].exists())

    def test_private_use_receipt_rejects_replaced_private_member(self) -> None:
        receipt, private_path = self._crash_after_private_use_receipt()
        private_path.unlink()
        private_path.write_bytes(b"foreign\n")
        private_path.chmod(0o600)
        replacement = private_path.stat()
        replacement_identity = (replacement.st_dev, replacement.st_ino)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "private-use retirement.*private member changed",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        current = private_path.stat()
        self.assertEqual((current.st_dev, current.st_ino), replacement_identity)
        self.assertEqual(private_path.read_bytes(), b"foreign\n")
        self.assertTrue(receipt.path.exists())
        self.assertTrue(receipt.path.with_name(receipt.cleanup_control.name).exists())
        self.assertTrue(
            receipt.path.with_name(receipt.allocation_control.name).exists()
        )

    def test_private_use_receipt_rejects_extra_private_member(self) -> None:
        receipt, private_path = self._crash_after_private_use_receipt()
        extra = private_path.parent / "foreign"
        extra.write_bytes(b"foreign\n")

        with self.assertRaisesRegex(MODULE.SyncError, "retirement leaf changed"):
            MODULE._cleanup_ready_pending_batches(self.home)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertEqual(extra.read_bytes(), b"foreign\n")
        self.assertTrue(receipt.path.exists())

    def test_private_use_receipt_malformed_descendant_blocks_recovery(self) -> None:
        receipt, private_path = self._crash_after_private_use_receipt()
        malformed = receipt.path.with_name(receipt.path.name + ".extra")
        malformed.write_bytes(b"foreign\n")
        malformed.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "retirement receipt representation must be reconciled",
        ):
            MODULE._cleanup_ready_pending_batches(self.home)

        self.assertEqual(private_path.read_bytes(), b'role = "reviewer"\n')
        self.assertTrue(receipt.path.exists())
        self.assertEqual(malformed.read_bytes(), b"foreign\n")

    def test_private_move_cannot_republish_reclaim_after_v8_retirement(self) -> None:
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            destination, moved, binding = (
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="v8-no-fallback",
                    expected_identity=self.source_identity,
                    retain_batch_binding=True,
                )
            )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertFalse(self.source.exists())
        self.assertEqual(moved.file_identity, self.source_identity)
        self.assertTrue(destination.is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join exact cleanup authority",
        ):
            MODULE._publish_pending_ephemeral_quarantine_cleanup_ticket(
                self.home,
                binding,
            )
        self.assertTrue(destination.is_file())

    def test_v8_to_v7_join_rejects_metadata_size_drift(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        assert binding.allocation_ticket is not None
        changed_ticket = MODULE.replace(
            binding.allocation_ticket,
            metadata_size=binding.allocation_ticket.metadata_size + 1,
        )
        changed_binding = MODULE.replace(
            binding,
            allocation_ticket=changed_ticket,
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join exact cleanup authority",
        ):
            MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                self.home,
                changed_binding,
            )

        self.assertTrue((batch_root / "metadata.json").is_file())
        self.assertTrue(binding.allocation_ticket.path.is_file())

    def test_v8_entity_with_extra_member_blocks_recovery(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.revoke_reclaim()
        allocation.close()
        (allocation.batch_root / "foreign").write_bytes(b"foreign\n")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._pending_cleanup_ready_batch_is_observed(self.home)

        dry_run_output = io.StringIO()
        with (
            contextlib.redirect_stdout(dry_run_output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "present entity without exact cleanup authority",
            ),
        ):
            MODULE._preflight_pending_recovery(
                self.home,
                dry_run=True,
            )
        self.assertNotIn("would clean", dry_run_output.getvalue())

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "present entity without exact cleanup authority",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )

        self.assertTrue(allocation.batch_root.is_dir())
        assert allocation.binding.allocation_ticket is not None
        self.assertTrue(allocation.binding.allocation_ticket.path.is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending quarantine allocation must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)

    def test_v7_ticket_temp_blocks_mutation_gate(self) -> None:
        temp_path = self._write_v7_ticket_temp()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "ticket temp must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)

        self.assertTrue(temp_path.is_file())

    def test_retained_v7_ticket_temp_blocks_mutation_gate(self) -> None:
        temp_path = self._write_v7_ticket_temp()
        retained_name = next(MODULE._retained_pending_cleanup_names(temp_path))
        temp_path.rename(temp_path.with_name(retained_name))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "ticket temp must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)

        self.assertTrue(temp_path.with_name(retained_name).is_file())

    def test_v8_retained_temp_is_promoted_then_ticket_only_retired(self) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        batch_name = f"20260905T020202Z-{os.getpid()}-{os.getpid()}"
        payload = MODULE._pending_quarantine_allocation_payload(
            batch_name,
            MODULE._directory_identity(quarantine_fd),
            b"{}\n",
        )
        MODULE._close_fd_quietly(quarantine_fd)
        path = MODULE._pending_quarantine_allocation_path(self.home, batch_name)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            path.parent,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        temp_path = path.with_name(
            batch_name + MODULE.PENDING_QUARANTINE_ALLOCATION_TEMP_SUFFIX
        )
        MODULE._write_exclusive_internal_file(self.home, temp_path, payload)
        retained_name = next(MODULE._retained_pending_cleanup_names(temp_path))
        temp_path.rename(temp_path.with_name(retained_name))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending quarantine allocation must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)
        self.assertEqual(
            MODULE._cleanup_pending_cleanup_ticket_temps(self.home, limit=4),
            0,
        )
        allocation = MODULE._read_pending_quarantine_allocation_ticket(
            self.home,
            path,
        )
        self.assertIsNotNone(allocation)
        self.assertEqual(
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            ),
            1,
        )
        self.assertFalse(path.exists())

    def test_v5_temp_with_present_v8_is_ready_in_status_and_preflight(self) -> None:
        batch_root, binding, temp_path = self._allocate_v5_ticket_representation(
            retained=False
        )
        assert binding.allocation_ticket is not None

        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        status_output = io.StringIO()
        with contextlib.redirect_stdout(status_output):
            self.assertFalse(MODULE.status(self.home))
        self.assertIn("must be cleaned", status_output.getvalue())
        dry_run_output = io.StringIO()
        with contextlib.redirect_stdout(dry_run_output):
            self.assertTrue(MODULE._preflight_pending_recovery(self.home, dry_run=True))
        self.assertIn("would clean", dry_run_output.getvalue())

        self.assertTrue(MODULE._preflight_pending_recovery(self.home, dry_run=False))

        self.assertFalse(batch_root.exists())
        self.assertFalse(temp_path.exists())
        self.assertFalse(binding.allocation_ticket.path.exists())

    def test_retained_v5_temp_with_present_v8_recovers_integrated(self) -> None:
        batch_root, binding, retained_path = self._allocate_v5_ticket_representation(
            retained=True
        )
        assert binding.allocation_ticket is not None

        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        dry_run_output = io.StringIO()
        with contextlib.redirect_stdout(dry_run_output):
            self.assertTrue(MODULE._preflight_pending_recovery(self.home, dry_run=True))
        self.assertIn("would clean", dry_run_output.getvalue())

        self.assertTrue(MODULE._preflight_pending_recovery(self.home, dry_run=False))

        self.assertFalse(batch_root.exists())
        self.assertFalse(retained_path.exists())
        self.assertFalse(binding.allocation_ticket.path.exists())

    def test_v7_temp_and_retained_ticket_precede_present_v8_classification(
        self,
    ) -> None:
        for representation in ("temp", "retained"):
            with self.subTest(representation=representation):
                batch_root, binding = self._allocate_unowned_scaffold()
                ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                    self.home, binding
                )
                if representation == "temp":
                    represented = ticket.path.with_name(
                        batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
                    )
                else:
                    represented = ticket.path.with_name(
                        next(MODULE._retained_pending_cleanup_names(ticket.path))
                    )
                ticket.path.rename(represented)
                assert binding.allocation_ticket is not None

                self.assertTrue(
                    MODULE._pending_cleanup_ready_batch_is_observed(self.home)
                )
                dry_run_output = io.StringIO()
                with contextlib.redirect_stdout(dry_run_output):
                    self.assertTrue(
                        MODULE._preflight_pending_recovery(self.home, dry_run=True)
                    )
                self.assertIn("would clean", dry_run_output.getvalue())
                self.assertTrue(
                    MODULE._preflight_pending_recovery(self.home, dry_run=False)
                )

                self.assertFalse(batch_root.exists())
                self.assertFalse(represented.exists())
                self.assertFalse(binding.allocation_ticket.path.exists())

    def test_metadata_tombstone_boundary_rejects_leaf_appearance(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="leaf",
        )

    def test_metadata_tombstone_boundary_rejects_ticket_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="ticket",
        )

    def test_metadata_tombstone_boundary_rejects_batch_policy_drift(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="batch-policy",
        )

    def test_metadata_tombstone_boundary_rejects_root_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="root-identity",
        )

    def test_metadata_unlink_boundary_rejects_leaf_appearance(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="leaf",
        )

    def test_metadata_unlink_boundary_rejects_ticket_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="ticket",
        )

    def test_metadata_unlink_boundary_rejects_batch_policy_drift(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="batch-policy",
        )

    def test_metadata_unlink_boundary_rejects_root_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="root-identity",
        )

    def test_leafless_cleanup_recovers_after_metadata_deletion_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def delete_metadata_then_crash(
            home: Path,
            path: Path,
            parent_fd: int,
            snapshot: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
            **kwargs: object,
        ) -> None:
            real_delete(home, path, parent_fd, snapshot, label=label, **kwargs)
            if path == batch_root / "metadata.json":
                raise SystemExit("injected metadata deletion crash")

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=delete_metadata_then_crash,
            ),
            self.assertRaisesRegex(SystemExit, "metadata deletion crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertFalse((batch_root / "metadata.json").exists())
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_cleanup_recovers_metadata_tombstone_after_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rename = MODULE._rename_noreplace_at

        def isolate_metadata_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == "metadata.json" and destination_name.startswith(
                MODULE.PENDING_CLEANUP_RETAINED_PREFIX + "metadata.json-"
            ):
                raise SystemExit("injected metadata tombstone crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=isolate_metadata_then_crash,
            ),
            self.assertRaisesRegex(SystemExit, "metadata tombstone crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertFalse((batch_root / "metadata.json").exists())
        self.assertEqual(
            len(
                list(
                    batch_root.glob(
                        MODULE.PENDING_CLEANUP_RETAINED_PREFIX + "metadata.json-*"
                    )
                )
            ),
            1,
        )
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_cleanup_recovers_after_batch_isolation_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rename = MODULE._rename_noreplace_at
        isolated_name = MODULE._pending_cleanup_isolated_batch_name(batch_root.name)

        def isolate_batch_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == batch_root.name and destination_name == isolated_name:
                raise SystemExit("injected batch isolation crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=isolate_batch_then_crash,
            ),
            self.assertRaisesRegex(SystemExit, "batch isolation crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertTrue(batch_root.with_name(isolated_name).is_dir())
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_cleanup_recovers_final_private_batch_isolation(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rename = MODULE._rename_noreplace_at
        isolated_name = MODULE._pending_cleanup_isolated_batch_name(batch_root.name)
        private_path: Path | None = None

        def isolate_private_batch_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal private_path
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == isolated_name and destination_name.startswith(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
            ):
                private_path = batch_root.parent / destination_name
                raise SystemExit("injected final private batch isolation crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=isolate_private_batch_then_crash,
            ),
            self.assertRaisesRegex(
                SystemExit,
                "final private batch isolation crash",
            ),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertIsNotNone(private_path)
        assert private_path is not None
        self.assertTrue(private_path.is_dir())
        self.assertFalse(batch_root.exists())
        self.assertFalse(batch_root.with_name(isolated_name).exists())
        self._resume_scaffold_cleanup(batch_root)
        self.assertFalse(private_path.exists())

    def test_leafless_cleanup_recovers_after_batch_rmdir_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rmdir = os.rmdir
        isolated_name = MODULE._pending_cleanup_isolated_batch_name(batch_root.name)

        def remove_batch_then_crash(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> None:
            real_rmdir(path, *args, **kwargs)
            if isinstance(path, str) and path.startswith(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
            ):
                raise SystemExit("injected batch rmdir crash")

        with (
            mock.patch.object(os, "rmdir", side_effect=remove_batch_then_crash),
            self.assertRaisesRegex(SystemExit, "batch rmdir crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertFalse(batch_root.exists())
        self.assertFalse(batch_root.with_name(isolated_name).exists())
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_ticket_temp_is_promoted_after_publication_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rename = MODULE._rename_noreplace_at

        def crash_before_ticket_publication(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            if (
                source_name
                == batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
                and destination_name
                == batch_root.name + MODULE.PENDING_CLEANUP_TICKET_SUFFIX
            ):
                raise SystemExit("injected ticket publication crash")
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_before_ticket_publication,
            ),
            self.assertRaisesRegex(SystemExit, "ticket publication crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        index = MODULE._pending_cleanup_index_path(self.home)
        self.assertTrue(
            (
                index / (batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX)
            ).is_file()
        )
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_ticket_rejects_leaf_appearance(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        (batch_root / "leaf").mkdir(mode=0o700)

        with self.assertRaisesRegex(MODULE.SyncError, "scaffold leaf appeared"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((batch_root / "leaf").is_dir())
        self.assertTrue(ticket.path.is_file())

    def test_leafless_ticket_rejects_metadata_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        metadata = batch_root / "metadata.json"
        original_metadata_fd = os.open(metadata, os.O_RDONLY)
        try:
            metadata.unlink()
            metadata.write_bytes(b'{"foreign": true}\n')
            metadata.chmod(0o600)
        finally:
            os.close(original_metadata_fd)

        with self.assertRaisesRegex(MODULE.SyncError, "changed before read"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertEqual(metadata.read_bytes(), b'{"foreign": true}\n')
        self.assertTrue(ticket.path.is_file())

    def test_leafless_ticket_rejects_batch_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        original = batch_root.with_name(batch_root.name + ".original")
        batch_root.rename(original)
        batch_root.mkdir(mode=0o700)
        (batch_root / "foreign").write_bytes(b"foreign\n")

        with self.assertRaisesRegex(MODULE.SyncError, "batch changed"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((original / "metadata.json").is_file())
        self.assertEqual((batch_root / "foreign").read_bytes(), b"foreign\n")
        self.assertTrue(ticket.path.is_file())

    def test_leafless_ticket_rejects_quarantine_root_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        quarantine_root = batch_root.parent
        original = quarantine_root.with_name(quarantine_root.name + ".original")
        quarantine_root.rename(original)
        quarantine_root.mkdir(mode=0o700)

        with self.assertRaisesRegex(MODULE.SyncError, "quarantine root changed"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((original / batch_root.name / "metadata.json").is_file())
        self.assertTrue(ticket.path.is_file())

    def test_leafless_cleanup_rejects_ticket_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        original_ticket_fd = os.open(ticket.path, os.O_RDONLY)
        try:
            ticket.path.unlink()
            ticket.path.write_bytes(b"{}\n")
            ticket.path.chmod(0o600)
        finally:
            os.close(original_ticket_fd)

        with self.assertRaisesRegex(MODULE.SyncError, "changed before read"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((batch_root / "metadata.json").is_file())
        self.assertEqual(ticket.path.read_bytes(), b"{}\n")

    def test_receiptless_cleanup_does_not_allocate_a_quarantine_batch(self) -> None:
        for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1):
            MODULE._quarantine_batch_root(self.home, [])
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            self.source,
            require_managed_access=False,
        )

        with mock.patch.object(
            MODULE,
            "_quarantine_batch_root",
            side_effect=AssertionError("receiptless cleanup allocated a batch"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                self.home,
                self.source,
                expected,
            )

        self.assertFalse(self.source.exists())
        self.assertEqual(
            MODULE._quarantine_batch_count(self.home),
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1,
        )
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_ticket_publication_failure_leaves_no_batch_scaffold(self) -> None:
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            self.source,
            require_managed_access=False,
        )
        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_batch_cleanup_ticket_for_root",
                side_effect=SystemExit("injected pre-publication crash"),
            ),
            self.assertRaisesRegex(SystemExit, "pre-publication crash"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                self.home,
                self.source,
                expected,
            )

        self._assert_source_was_not_moved()
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)


if __name__ == "__main__":
    unittest.main()
