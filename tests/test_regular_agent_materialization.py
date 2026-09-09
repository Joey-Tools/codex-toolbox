from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_regular_agent_materialization",
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


def write_release(
    root: Path,
    *,
    owner: str = MODULE.PUBLIC_OWNER,
    role_payload: str | None = None,
    target: str = "agents/reviewer.toml",
    override: bool = False,
    base_sha: str | None = None,
) -> None:
    links: list[dict[str, object]] = []
    if role_payload is not None:
        source = root / "personal_codex" / "agents" / "reviewer.toml"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(role_payload, encoding="utf-8")
        entry: dict[str, object] = {
            "source": "personal_codex/agents/reviewer.toml",
            "target": target,
            "kind": "file",
            "owner": owner,
        }
        if override:
            entry["override"] = True
        links.append(entry)
    else:
        source = root / "personal_codex" / "skills" / "base" / "SKILL.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# Base\n", encoding="utf-8")
        links.append(
            {
                "source": "personal_codex/skills/base",
                "target": "skills/base",
                "kind": "skill",
                "owner": owner,
            }
        )
    payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": links,
    }
    if base_sha is not None:
        payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_sha,
        }
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def write_regular_to_symlink_release(
    root: Path,
    *,
    kind: str = "skill",
    source_path: str = "personal_codex/skills/reviewer",
    removed_source: str = "personal_codex/agents/reviewer.toml",
    removed_kind: str = "file",
    replacement_target: str = "agents/reviewer.toml",
    include_removal: bool = True,
) -> None:
    source = root / source_path
    source.mkdir(parents=True, exist_ok=True)
    (source / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
    payload: dict[str, object] = {
        "version": 1,
        "owner": MODULE.PUBLIC_OWNER,
        "links": [
            {
                "source": source_path,
                "target": ROLE_TARGET.as_posix(),
                "kind": kind,
                "owner": MODULE.PUBLIC_OWNER,
            }
        ],
    }
    if include_removal:
        payload["removed_links"] = [
            {
                "id": "regular-agent-to-symlink",
                "source": removed_source,
                "target": ROLE_TARGET.as_posix(),
                "kind": removed_kind,
                "replacement_target": replacement_target,
            }
        ]
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def append_regular_link(
    root: Path,
    *,
    target: str,
    source: str = "personal_codex/agents/reviewer.toml",
    payload: str | None = None,
) -> None:
    if payload is not None:
        source_path = root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(payload, encoding="utf-8")
    manifest_path = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["links"].append(
        {
            "source": source,
            "target": target,
            "kind": "file",
            "owner": manifest["owner"],
        }
    )
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")


def install(root: Path, home: Path, sha: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, sha, dry_run=False)


def downgrade_pending_state_evidence_metadata(
    payload: dict[str, object],
    version: int,
) -> None:
    if version >= 9:
        return
    for field in ("state_before", "state_after", "commit_evidence"):
        evidence = payload[field]
        assert isinstance(evidence, dict)
        evidence.pop("uid")
        evidence.pop("gid")


def legacy_v6_writer_metadata_payload(
    batch: MODULE.PendingLinkBatch,
) -> tuple[dict[str, object], int | None]:
    metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["version"] == MODULE.PENDING_LINK_METADATA_VERSION
    payload["version"] = 6
    downgrade_pending_state_evidence_metadata(payload, 6)
    payload.pop("terminal_regular_before")
    payload.pop("terminal_regular_after")
    legacy_gid: int | None = None
    records = payload["records"]
    assert isinstance(records, list)
    for raw_record in records:
        assert isinstance(raw_record, dict)
        raw_record.pop("before_materialization")
        raw_record.pop("removed_link")
        raw_record.pop("publication_cleanup")
        if raw_record["materialization"] == "regular" and raw_record["action"] in {
            "create",
            "replace",
            "quarantine-replace",
        }:
            stage = raw_record["stage"]
            evidence = raw_record["evidence"]
            assert isinstance(stage, str)
            assert isinstance(evidence, str)
            stage_metadata = os.stat(batch.batch_root / stage)
            evidence_metadata = os.stat(batch.batch_root / evidence)
            assert (stage_metadata.st_dev, stage_metadata.st_ino) == tuple(
                raw_record["stage_identity"]
            )
            assert (evidence_metadata.st_dev, evidence_metadata.st_ino) == tuple(
                raw_record["evidence_identity"]
            )
            assert (stage_metadata.st_dev, stage_metadata.st_ino) == (
                evidence_metadata.st_dev,
                evidence_metadata.st_ino,
            )
            assert stat.S_IMODE(stage_metadata.st_mode) == raw_record["regular_mode"]
            assert stage_metadata.st_uid == raw_record["regular_uid"]
            assert stage_metadata.st_nlink in {
                raw_record["regular_link_count"],
                raw_record["regular_link_count"] + 1,
            }
            assert evidence_metadata.st_nlink == stage_metadata.st_nlink
            assert raw_record["regular_gid"] is None
            raw_record["regular_gid"] = stage_metadata.st_gid
            legacy_gid = stage_metadata.st_gid
        elif raw_record["materialization"] == "regular":
            assert raw_record["action"] in {"remove", "quarantine-remove"}
            assert raw_record["regular_gid"] is None
            planned = raw_record["planned_before"]
            assert isinstance(planned, dict)
            planned_gid = planned["regular_gid"]
            assert isinstance(planned_gid, int)
            assert not isinstance(planned_gid, bool)
            assert planned_gid >= 0
    return payload, legacy_gid


def write_pending_metadata_payload(
    batch: MODULE.PendingLinkBatch,
    payload: dict[str, object],
) -> None:
    metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
    metadata_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def status_is_unhealthy(home: Path) -> bool:
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            return not MODULE.status(home)
        except MODULE.SyncError:
            return True


class PublicRegularAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.release = self.root / "release"
        self.payload = 'name = "reviewer"\n'
        write_release(self.release, role_payload=self.payload)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install(self) -> Path:
        install(self.release, self.home, SHA_A)
        return self.home / ROLE_TARGET

    def test_exact_same_target_removal_migrates_regular_agent_to_symlink(self) -> None:
        for kind in ("directory", "skill"):
            with self.subTest(kind=kind):
                home = self.root / f"home-{kind}"
                replacement = self.root / f"replacement-{kind}"
                write_regular_to_symlink_release(replacement, kind=kind)
                install(self.release, home, SHA_A)
                target = home / ROLE_TARGET
                old_identity = (target.stat().st_dev, target.stat().st_ino)

                install(replacement, home, SHA_B)

                expected_entry = MODULE.load_manifest_data(replacement).entries[0]
                self.assertTrue(target.is_symlink())
                self.assertEqual(
                    target.readlink().as_posix(),
                    MODULE._desired_link_target(home, expected_entry),
                )
                self.assertNotEqual(
                    (target.lstat().st_dev, target.lstat().st_ino),
                    old_identity,
                )
                state = MODULE._load_managed_state(home)
                self.assertEqual(state.links[ROLE_TARGET].kind, kind)
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(home))
                )

    def test_regular_to_symlink_migration_requires_exact_removal_authority(
        self,
    ) -> None:
        cases = (
            ("missing", {"include_removal": False}),
            ("source", {"removed_source": "personal_codex/agents/other.toml"}),
            ("kind", {"removed_kind": "directory"}),
        )
        for label, options in cases:
            with self.subTest(label=label):
                home = self.root / f"home-refusal-{label}"
                replacement = self.root / f"replacement-refusal-{label}"
                write_regular_to_symlink_release(replacement, **options)
                install(self.release, home, SHA_A)
                target = home / ROLE_TARGET

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "without an exact same-target removal",
                ):
                    install(replacement, home, SHA_B)

                self.assertTrue(target.is_file())
                self.assertFalse(target.is_symlink())
                self.assertEqual(target.read_text(encoding="utf-8"), self.payload)

    def test_regular_state_rejects_substituted_matching_symlink_without_authority(
        self,
    ) -> None:
        replacement = self.root / "replacement-substituted-matching-symlink"
        write_regular_to_symlink_release(
            replacement,
            source_path="personal_codex/agents/reviewer.toml",
            include_removal=False,
        )
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        desired_entry = MODULE.load_manifest_data(replacement).entries[0]
        desired = MODULE._desired_link_target(self.home, desired_entry)
        before_state = MODULE._load_managed_state(self.home)
        self.assertEqual(before_state.links[ROLE_TARGET].link_target, desired)
        target.unlink()
        target.symlink_to(desired)
        substituted_identity = (target.lstat().st_dev, target.lstat().st_ino)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed state target type mismatch for regular file",
        ):
            install(replacement, self.home, SHA_B)

        self.assertTrue(target.is_symlink())
        self.assertEqual(target.readlink().as_posix(), desired)
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            substituted_identity,
        )
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners[MODULE.PUBLIC_OWNER], SHA_A)
        self.assertEqual(state.links[ROLE_TARGET].kind, "file")

    def test_regular_to_symlink_migration_rejects_content_and_access_drift(
        self,
    ) -> None:
        replacement = self.root / "replacement-drift"
        write_regular_to_symlink_release(replacement)
        for drift in ("content", "access"):
            with self.subTest(drift=drift):
                home = self.root / f"home-drift-{drift}"
                install(self.release, home, SHA_A)
                target = home / ROLE_TARGET
                if drift == "content":
                    target.write_text('name = "modified"\n', encoding="utf-8")
                    expected = "modified managed regular file"
                else:
                    target.chmod(0o644)
                    expected = "access policy mismatch"

                with self.assertRaisesRegex(MODULE.SyncError, expected):
                    install(replacement, home, SHA_B)

                self.assertTrue(target.is_file())
                self.assertFalse(target.is_symlink())

    def test_public_install_and_status_accept_exact_independent_file(self) -> None:
        target = self._install()

        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), self.payload)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(MODULE.status(self.home))

    def test_create_fstat_failure_evacuates_unbound_toml_to_private_quarantine(
        self,
    ) -> None:
        self.home.mkdir()
        target = self.home / ROLE_TARGET
        target.parent.mkdir()
        source = self.release / "personal_codex" / "agents" / "reviewer.toml"
        real_open = os.open
        real_fstat = os.fstat
        real_rename_noreplace = MODULE._rename_noreplace_at
        created_fd: int | None = None
        created_parent_fd: int | None = None
        injected = False
        canonical_isolated = False

        def capture_created_fd(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal created_fd, created_parent_fd
            file_descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == target.name and flags & os.O_CREAT:
                created_fd = file_descriptor
                created_parent_fd = dir_fd
            return file_descriptor

        def fail_initial_created_fstat(file_descriptor: int) -> os.stat_result:
            nonlocal injected
            if file_descriptor == created_fd and not injected:
                injected = True
                raise OSError("injected created-file fstat failure")
            if injected and not canonical_isolated:
                raise OSError("cleanup attempted fstat before canonical isolation")
            return real_fstat(file_descriptor)

        def observe_canonical_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal canonical_isolated
            real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                source_parent_fd == created_parent_fd
                and destination_parent_fd == created_parent_fd
                and source_name == target.name
            ):
                canonical_isolated = True

        with (
            mock.patch.object(MODULE.os, "open", side_effect=capture_created_fd),
            mock.patch.object(
                MODULE.os,
                "fstat",
                side_effect=fail_initial_created_fstat,
            ),
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=observe_canonical_isolation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exact cleanup could not be verified",
            ),
        ):
            MODULE._create_regular_file_beneath(self.home, source, target)

        self.assertTrue(injected)
        self.assertTrue(canonical_isolated)
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(tuple(target.parent.glob("*.toml")), ())
        retained = tuple((self.home / "personal-sync" / "quarantine").glob("*/leaf/*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"")

    def test_unbound_create_cleanup_retains_replacement_without_deleting_it(
        self,
    ) -> None:
        self.home.mkdir()
        target = self.home / ROLE_TARGET
        target.parent.mkdir()
        source = self.release / "personal_codex" / "agents" / "reviewer.toml"
        real_open = os.open
        real_fstat = os.fstat
        created_fd: int | None = None
        created_parent_fd: int | None = None
        replacement_identity: tuple[int, int] | None = None
        injected = False

        def capture_created_fd(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal created_fd, created_parent_fd
            file_descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == target.name and flags & os.O_CREAT:
                created_fd = file_descriptor
                created_parent_fd = dir_fd
            return file_descriptor

        def replace_before_failed_identity_binding(
            file_descriptor: int,
        ) -> os.stat_result:
            nonlocal injected, replacement_identity
            if file_descriptor != created_fd or injected:
                return real_fstat(file_descriptor)
            injected = True
            assert created_parent_fd is not None
            os.unlink(target.name, dir_fd=created_parent_fd)
            replacement_fd = real_open(
                target.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=created_parent_fd,
            )
            try:
                os.write(replacement_fd, b"foreign")
                replacement = real_fstat(replacement_fd)
                replacement_identity = (replacement.st_dev, replacement.st_ino)
            finally:
                os.close(replacement_fd)
            raise OSError("injected created-file fstat failure after replacement")

        with (
            mock.patch.object(MODULE.os, "open", side_effect=capture_created_fd),
            mock.patch.object(
                MODULE.os,
                "fstat",
                side_effect=replace_before_failed_identity_binding,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exact cleanup could not be verified",
            ),
        ):
            MODULE._create_regular_file_beneath(self.home, source, target)

        self.assertTrue(injected)
        self.assertIsNotNone(replacement_identity)
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(tuple(target.parent.glob("*.toml")), ())
        retained = tuple((self.home / "personal-sync" / "quarantine").glob("*/leaf/*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"foreign")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            replacement_identity,
        )

    def test_status_fails_closed_for_missing_type_content_mode_and_nlink_drift(
        self,
    ) -> None:
        mutations = ("missing", "type", "content", "mode", "nlink")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                case_home = self.root / f"home-{mutation}"
                install(self.release, case_home, SHA_A)
                target = case_home / ROLE_TARGET
                if mutation == "missing":
                    target.unlink()
                elif mutation == "type":
                    target.unlink()
                    target.symlink_to("../foreign/reviewer.toml")
                elif mutation == "content":
                    target.write_text("modified = true\n", encoding="utf-8")
                elif mutation == "mode":
                    target.chmod(0o644)
                else:
                    os.link(target, target.with_name("reviewer-copy.toml"))

                self.assertTrue(status_is_unhealthy(case_home))

    def test_failed_evidence_publication_retains_last_alias_after_source_swap(
        self,
    ) -> None:
        source = self.home / "personal-sync" / "source" / "authority"
        destination = self.home / "personal-sync" / "evidence" / "published"
        source.parent.mkdir(parents=True)
        destination.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        source_parent_fd = MODULE._open_directory_beneath(
            self.home,
            source.parent,
        )
        try:
            expected = MODULE._read_managed_state_file_snapshot(
                self.home,
                source,
                source_parent_fd,
            )
        finally:
            MODULE._close_fd_quietly(source_parent_fd)
        assert expected.file_identity is not None
        real_link = os.link

        def replace_source_after_link(*args: object, **kwargs: object) -> None:
            real_link(*args, **kwargs)  # type: ignore[arg-type]
            source.unlink()
            source.write_bytes(b"foreign")
            source.chmod(0o600)

        with (
            mock.patch.object(
                MODULE.os,
                "link",
                side_effect=replace_source_after_link,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained without deletion",
            ) as raised,
        ):
            MODULE._publish_regular_hardlink_beneath(
                self.home,
                source,
                destination,
                expected,
            )

        self.assertEqual(
            raised.exception.code,
            MODULE.PENDING_REGULAR_PUBLICATION_RETAINED_CODE,
        )
        self.assertEqual(destination.read_bytes(), b"authority")
        self.assertEqual(
            (destination.stat().st_dev, destination.stat().st_ino),
            expected.file_identity,
        )
        self.assertEqual(destination.stat().st_nlink, 1)
        self.assertEqual(source.read_bytes(), b"foreign")
        self.assertNotEqual(
            (source.stat().st_dev, source.stat().st_ino),
            expected.file_identity,
        )

    def test_post_link_failure_never_enters_path_revalidation_cleanup_window(
        self,
    ) -> None:
        source = self.home / "personal-sync" / "source" / "authority"
        destination = self.home / "personal-sync" / "evidence" / "published"
        source.parent.mkdir(parents=True)
        destination.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        source_parent_fd = MODULE._open_directory_beneath(
            self.home,
            source.parent,
        )
        try:
            expected = MODULE._read_managed_state_file_snapshot(
                self.home,
                source,
                source_parent_fd,
            )
        finally:
            MODULE._close_fd_quietly(source_parent_fd)
        real_cleanup = MODULE._isolate_and_delete_pending_cleanup_file

        def swap_source_then_delete(*args: object, **kwargs: object) -> None:
            source.unlink()
            source.write_bytes(b"foreign")
            source.chmod(0o600)
            real_cleanup(*args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(
                MODULE.os,
                "fsync",
                side_effect=OSError("injected post-link failure"),
            ),
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=swap_source_then_delete,
            ) as cleanup,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained without deletion",
            ),
        ):
            MODULE._publish_regular_hardlink_beneath(
                self.home,
                source,
                destination,
                expected,
            )

        cleanup.assert_not_called()
        self.assertEqual(source.read_bytes(), b"authority")
        self.assertEqual(destination.read_bytes(), b"authority")
        self.assertEqual(
            (source.stat().st_dev, source.stat().st_ino),
            (destination.stat().st_dev, destination.stat().st_ino),
        )

    def test_reconcile_snapshot_failure_clears_active_toml_before_error(
        self,
    ) -> None:
        stage = self.home / "personal-sync" / "stage" / "00000000"
        target = self.home / ROLE_TARGET
        stage.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        stage.write_bytes(b"authority")
        stage.chmod(0o600)
        stage_snapshot = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            stage,
            require_managed_access=False,
        )
        target_plan = MODULE._capture_reconcile_target_snapshot(self.home, target)
        real_snapshot = MODULE._regular_file_snapshot_at
        real_link = os.link
        linked = False
        failed = False

        def mark_linked(*args: object, **kwargs: object) -> None:
            nonlocal linked
            real_link(*args, **kwargs)  # type: ignore[arg-type]
            linked = True

        def fail_first_canonical_snapshot(
            directory_fd: int,
            name: str,
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal failed
            if linked and path == target and not failed:
                failed = True
                raise OSError("injected canonical snapshot failure")
            return real_snapshot(
                directory_fd,
                name,
                path,
                *args,
                **kwargs,
            )

        with (
            mock.patch.object(MODULE.os, "link", side_effect=mark_linked),
            mock.patch.object(
                MODULE,
                "_regular_file_snapshot_at",
                side_effect=fail_first_canonical_snapshot,
            ),
            self.assertRaisesRegex(OSError, "injected canonical snapshot failure"),
        ):
            MODULE._publish_regular_reconcile_hardlink_beneath(
                self.home,
                stage,
                target,
                stage_snapshot,
                target_plan,
                {},
            )

        self.assertFalse(os.path.lexists(target))
        self.assertTrue(failed)
        self.assertEqual(stage.read_bytes(), b"authority")
        self.assertEqual(stage.stat().st_nlink, 1)
        internal = tuple(
            child
            for child in target.parent.iterdir()
            if child.name.startswith(
                (
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                    MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX,
                )
            )
        )
        self.assertEqual(internal, ())

    def test_exact_publication_cleanup_quarantines_target_replacement(
        self,
    ) -> None:
        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )
        real_rename_noreplace = MODULE._rename_noreplace_at
        replaced = False
        replacement_identity: tuple[int, int] | None = None

        def replace_alias_at_private_move(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal replaced, replacement_identity
            if (
                source_name.startswith(".codex-publication-cleanup-")
                and destination_name.startswith(".codex-ephemeral-cleanup-")
                and not replaced
            ):
                os.unlink(source_name, dir_fd=source_parent_fd)
                replacement_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(replacement_fd, b"foreign")
                    replacement = os.fstat(replacement_fd)
                    replacement_identity = (
                        replacement.st_dev,
                        replacement.st_ino,
                    )
                finally:
                    os.close(replacement_fd)
                replaced = True
            real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=replace_alias_at_private_move,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained as isolated evidence",
            ),
        ):
            MODULE._delete_exact_regular_publication_beneath(
                self.home,
                target,
                expected,
            )

        self.assertTrue(replaced)
        self.assertIsNotNone(replacement_identity)
        self.assertFalse(os.path.lexists(target))
        retained = tuple(
            (self.home / "personal-sync" / "quarantine").glob(
                ".codex-ephemeral-cleanup-*"
            )
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"foreign")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            replacement_identity,
        )
        self.assertEqual(source.read_bytes(), b"authority")
        self.assertEqual(source.stat().st_nlink, 1)

    def test_exact_publication_cleanup_privately_isolates_initial_foreign_canonical(
        self,
    ) -> None:
        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )
        target.unlink()
        target.write_bytes(b"foreign canonical")
        target.chmod(0o600)
        replacement_identity = (
            target.stat().st_dev,
            target.stat().st_ino,
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "retained replacement as isolated evidence",
        ):
            MODULE._delete_exact_regular_publication_beneath(
                self.home,
                target,
                expected,
            )

        self.assertFalse(os.path.lexists(target))
        retained = tuple(
            (self.home / "personal-sync" / "quarantine").glob(
                ".codex-ephemeral-cleanup-*"
            )
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"foreign canonical")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            replacement_identity,
        )
        self.assertEqual(source.read_bytes(), b"authority")
        self.assertEqual(source.stat().st_nlink, 1)

    def test_exact_publication_cleanup_retains_private_authority_when_target_reappears(
        self,
    ) -> None:
        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )
        real_rename_noreplace = MODULE._rename_noreplace_at
        reappeared = False
        replacement_identity: tuple[int, int] | None = None

        def recreate_target_after_public_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal reappeared, replacement_identity
            real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name != target.name or reappeared:
                return
            replacement_fd = os.open(
                target.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=source_parent_fd,
            )
            try:
                os.write(replacement_fd, b"foreign")
                metadata = os.fstat(replacement_fd)
                replacement_identity = (metadata.st_dev, metadata.st_ino)
            finally:
                os.close(replacement_fd)
            reappeared = True

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=recreate_target_after_public_isolation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "public canonical reappeared after private isolation",
            ),
        ):
            MODULE._delete_exact_regular_publication_beneath(
                self.home,
                target,
                expected,
            )

        self.assertTrue(reappeared)
        self.assertIsNotNone(replacement_identity)
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            replacement_identity,
        )
        retained = tuple(
            (self.home / "personal-sync" / "quarantine").glob(
                ".codex-ephemeral-cleanup-*"
            )
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"authority")
        self.assertEqual(
            (
                retained[0].stat().st_dev,
                retained[0].stat().st_ino,
            ),
            (source.stat().st_dev, source.stat().st_ino),
        )
        self.assertEqual(source.stat().st_nlink, 2)

    def test_exact_publication_cleanup_reports_reappearance_during_private_delete(
        self,
    ) -> None:
        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )
        real_unlink = MODULE.os.unlink
        reappeared = False
        replacement_identity: tuple[int, int] | None = None

        def recreate_target_before_private_delete(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal reappeared, replacement_identity
            if name.startswith(".codex-ephemeral-cleanup-") and not reappeared:
                real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]
                target.write_bytes(b"foreign")
                target.chmod(0o600)
                metadata = target.stat()
                replacement_identity = (metadata.st_dev, metadata.st_ino)
                reappeared = True
                return
            real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=recreate_target_before_private_delete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "public canonical reappeared after private isolation",
            ),
        ):
            MODULE._delete_exact_regular_publication_beneath(
                self.home,
                target,
                expected,
            )

        self.assertTrue(reappeared)
        self.assertIsNotNone(replacement_identity)
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            replacement_identity,
        )
        retained = tuple(
            (self.home / "personal-sync" / "quarantine").glob(
                ".codex-ephemeral-cleanup-*"
            )
        )
        self.assertEqual(retained, ())
        self.assertEqual(source.read_bytes(), b"authority")
        self.assertEqual(source.stat().st_nlink, 1)

    def test_receiptless_cleanup_releases_quarantine_capacity(self) -> None:
        self.home.mkdir()
        for _index in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1):
            MODULE._quarantine_batch_root(self.home, [])
        self.assertEqual(
            MODULE._quarantine_batch_count(self.home),
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1,
        )

        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )

        MODULE._delete_exact_regular_publication_beneath(
            self.home,
            target,
            expected,
        )

        self.assertFalse(os.path.lexists(target))
        self.assertEqual(source.stat().st_nlink, 1)
        self.assertEqual(
            MODULE._quarantine_batch_count(self.home),
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1,
        )
        self.assertIsInstance(MODULE._quarantine_batch_root(self.home, []), Path)

    def test_receiptless_cleanup_retains_batch_when_leaf_gains_sibling(self) -> None:
        self.home.mkdir()
        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )
        real_unlink = MODULE.os.unlink
        sibling_identity: tuple[int, int] | None = None
        cleanup_batch_name: str | None = None
        added_sibling = False

        def add_sibling_after_leaf_unlink(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal added_sibling, cleanup_batch_name, sibling_identity
            real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]
            if not name.startswith(".codex-ephemeral-cleanup-") or added_sibling:
                return
            directory_fd = kwargs.get("dir_fd")
            assert isinstance(directory_fd, int)
            cleanup_batch_name = name.removeprefix(".codex-ephemeral-cleanup-").split(
                ".delete-", 1
            )[0]
            sibling_name = f"{name}-retained-0"
            sibling_fd = os.open(
                sibling_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(sibling_fd, b"foreign")
                sibling = os.fstat(sibling_fd)
                sibling_identity = (sibling.st_dev, sibling.st_ino)
            finally:
                os.close(sibling_fd)
            added_sibling = True

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=add_sibling_after_leaf_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained replacement as isolated evidence",
            ),
        ):
            MODULE._delete_exact_regular_publication_beneath(
                self.home,
                target,
                expected,
            )

        self.assertTrue(added_sibling)
        self.assertIsNotNone(cleanup_batch_name)
        assert cleanup_batch_name is not None
        self.assertIsNotNone(sibling_identity)
        self.assertFalse(os.path.lexists(target))
        siblings = tuple(
            (self.home / "personal-sync" / "quarantine").glob(
                ".codex-ephemeral-cleanup-*-retained-0"
            )
        )
        self.assertEqual(len(siblings), 1)
        self.assertEqual(
            (siblings[0].stat().st_dev, siblings[0].stat().st_ino),
            sibling_identity,
        )
        self.assertEqual(siblings[0].read_bytes(), b"foreign")
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        self.assertTrue(
            MODULE._pending_cleanup_ticket_path(
                self.home,
                cleanup_batch_name,
            ).is_file()
        )
        self.assertTrue(
            MODULE._pending_cleanup_terminal_validation_path(
                self.home,
                cleanup_batch_name,
            ).is_file()
        )

    def test_receiptless_cleanup_retains_same_inode_tombstone_derivative(
        self,
    ) -> None:
        self.home.mkdir()
        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )
        real_unlink = MODULE.os.unlink
        retained_identity: tuple[int, int] | None = None
        retained_name: str | None = None

        def retain_same_inode_before_leaf_unlink(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal retained_identity, retained_name
            directory_fd = kwargs.get("dir_fd")
            if (
                ".delete-" in name
                and retained_name is None
                and isinstance(directory_fd, int)
            ):
                retained_name = f"{name}-retained-0"
                os.link(
                    name,
                    retained_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                retained = os.stat(
                    retained_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                retained_identity = (retained.st_dev, retained.st_ino)
            real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=retain_same_inode_before_leaf_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "retained replacement as isolated evidence",
            ),
        ):
            MODULE._delete_exact_regular_publication_beneath(
                self.home,
                target,
                expected,
            )

        self.assertIsNotNone(retained_name)
        assert retained_name is not None
        self.assertIsNotNone(retained_identity)
        retained_path = self.home / "personal-sync" / "quarantine" / retained_name
        self.assertTrue(retained_path.is_file())
        self.assertEqual(retained_path.read_bytes(), b"authority")
        self.assertEqual(
            (retained_path.stat().st_dev, retained_path.stat().st_ino),
            retained_identity,
        )
        self.assertEqual(
            retained_identity,
            (source.stat().st_dev, source.stat().st_ino),
        )

    def test_receiptless_cleanup_reclaims_empty_setup_batch_after_leaf_failure(
        self,
    ) -> None:
        self.home.mkdir()
        source = self.home / "personal-sync" / "source" / "authority"
        target = self.home / ROLE_TARGET
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"authority")
        source.chmod(0o600)
        os.link(source, target, follow_symlinks=False)
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            target,
            require_managed_access=False,
        )
        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_batch_cleanup_ticket_for_root",
                side_effect=MODULE.SyncError(
                    "injected ephemeral ticket publication failure"
                ),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected ephemeral ticket publication failure",
            ),
        ):
            MODULE._delete_exact_regular_publication_beneath(
                self.home,
                target,
                expected,
            )

        self.assertTrue(target.is_file())
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            (source.stat().st_dev, source.stat().st_ino),
        )
        self.assertEqual(source.stat().st_nlink, 2)
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        quarantine_root = self.home / "personal-sync" / "quarantine"
        self.assertFalse(tuple(quarantine_root.glob(".codex-ephemeral-cleanup-*")))

    def test_desired_entry_verification_rejects_exact_target_symlink_for_regular_file(
        self,
    ) -> None:
        target = self._install()
        entry = MODULE.load_manifest(self.release)[0]
        target.unlink()
        target.symlink_to(MODULE._desired_link_target(self.home, entry))

        with (
            mock.patch.object(
                MODULE,
                "_read_optional_symlink_target_beneath",
                wraps=MODULE._read_optional_symlink_target_beneath,
            ) as read_symlink,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed link verification failed",
            ),
        ):
            MODULE._verify_desired_entries(self.home, [entry])

        read_symlink.assert_not_called()

    def test_committed_state_rejects_exact_target_symlink_for_regular_file(
        self,
    ) -> None:
        target = self._install()
        entry = MODULE.load_manifest(self.release)[0]
        target.unlink()
        target.symlink_to(MODULE._desired_link_target(self.home, entry))

        with (
            mock.patch.object(
                MODULE,
                "_read_optional_symlink_target_beneath",
                wraps=MODULE._read_optional_symlink_target_beneath,
            ) as read_symlink,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "mandatory desired regular file drifted",
            ),
        ):
            MODULE._committed_state(
                self.home,
                [entry],
                {MODULE.PUBLIC_OWNER: SHA_A},
            )

        read_symlink.assert_not_called()

    def test_exact_noop_rejects_raced_exact_target_symlink_for_regular_file(
        self,
    ) -> None:
        target = self._install()
        entry = MODULE.load_manifest(self.release)[0]
        real_verify = MODULE._verify_desired_entries
        raced = False

        def replace_before_noop_verification(
            home: Path,
            desired_entries: list[MODULE.LinkEntry],
            *,
            pending_batch: MODULE.PendingLinkBatch | None = None,
        ) -> None:
            nonlocal raced
            if not raced:
                target.unlink()
                target.symlink_to(MODULE._desired_link_target(home, entry))
                raced = True
            real_verify(home, desired_entries, pending_batch=pending_batch)

        with (
            mock.patch.object(
                MODULE,
                "_verify_desired_entries",
                side_effect=replace_before_noop_verification,
            ),
            mock.patch.object(MODULE, "_stage_pending_link_batch") as stage,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed link verification failed",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(raced)
        stage.assert_not_called()
        self.assertTrue(target.is_symlink())

    def test_public_manifest_transition_removes_regular_role_and_ledger_claim(
        self,
    ) -> None:
        target = self._install()
        next_release = self.root / "next-release"
        write_release(next_release)

        install(next_release, self.home, SHA_B)

        self.assertFalse(os.path.lexists(target))
        state = MODULE._load_managed_state(self.home)
        self.assertNotIn(ROLE_TARGET, state.links)

    def test_portable_agent_target_aliases_materialize_regular_files(self) -> None:
        aliases = ("Agents/reviewer.toml", "agents/REVIEWER.TOML")
        for index, alias in enumerate(aliases):
            with self.subTest(alias=alias):
                release = self.root / f"alias-release-{index}"
                home = self.root / f"alias-home-{index}"
                write_release(release, role_payload=self.payload, target=alias)

                install(release, home, SHA_A)

                target = home / Path(alias)
                self.assertTrue(target.is_file())
                self.assertFalse(target.is_symlink())
                self.assertEqual(target.read_text(encoding="utf-8"), self.payload)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


class RegularAgentMaterializationBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.release = self.root / "release"
        self.payload = 'name = "shared"\n'
        write_release(self.release, role_payload=self.payload)
        append_regular_link(
            self.release,
            target="agents/security-reviewer.toml",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan_initial_actions(self) -> list[MODULE.ReconcileAction]:
        entries = MODULE.load_manifest(self.release)
        incoming_sources = {
            (entry.owner, entry.target): self.release / Path(*entry.source.parts)
            for entry in entries
            if MODULE._entry_materializes_regular_file(entry)
        }
        return MODULE._plan_reconciliation(
            self.home,
            entries,
            [],
            [],
            MODULE.ManagedState(owners={}, links={}),
            allow_cross_owner=False,
            owner_shas={MODULE.PUBLIC_OWNER: SHA_A},
            incoming_regular_sources=incoming_sources,
        )

    def test_budget_charges_each_producing_target_even_for_shared_source(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                len(self.payload.encode("utf-8")),
            ),
            mock.patch.object(
                MODULE,
                "_create_regular_file_beneath",
                wraps=MODULE._create_regular_file_beneath,
            ) as create_regular,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular.*materialization.*(limit|budget)|materialization.*bytes",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(create_regular.call_count, 0)
        self.assertFalse(os.path.lexists(self.home / ROLE_TARGET))
        self.assertFalse(
            os.path.lexists(self.home / "agents" / "security-reviewer.toml")
        )

        with mock.patch.object(
            MODULE,
            "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
            2 * len(self.payload.encode("utf-8")),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual((self.home / ROLE_TARGET).read_text(), self.payload)
        self.assertEqual(
            (self.home / "agents" / "security-reviewer.toml").read_text(),
            self.payload,
        )

    def test_dry_run_uses_the_same_materialization_capacity_gate(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                len(self.payload.encode("utf-8")),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular.*materialization.*(limit|budget)|materialization.*bytes",
            ),
        ):
            MODULE.install_release_tree(
                self.release,
                self.home,
                SHA_A,
                dry_run=True,
            )

    def test_absent_create_rejects_before_reading_regular_payload(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                0,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_source_payload",
                wraps=MODULE._read_regular_source_payload,
            ) as read_payload,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular.*materialization.*(limit|budget)|materialization.*bytes",
            ),
        ):
            self._plan_initial_actions()

        self.assertEqual(read_payload.call_count, 0)

    def test_shared_source_is_read_once_under_independent_evidence_budget(self) -> None:
        payload_size = len(self.payload.encode("utf-8"))
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                2 * payload_size,
            ),
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_EVIDENCE_READ_BYTES",
                payload_size,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_source_payload",
                wraps=MODULE._read_regular_source_payload,
            ) as read_payload,
        ):
            actions = self._plan_initial_actions()

        self.assertEqual(len(actions), 2)
        self.assertEqual(read_payload.call_count, 1)

    def test_distinct_source_evidence_reads_have_an_aggregate_budget(self) -> None:
        second_source = "personal_codex/agents/security-reviewer.toml"
        append_regular_link(
            self.release,
            target="agents/security-reviewer-alt.toml",
            source=second_source,
            payload=self.payload,
        )
        payload_size = len(self.payload.encode("utf-8"))
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                3 * payload_size,
            ),
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_EVIDENCE_READ_BYTES",
                payload_size,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_source_payload",
                wraps=MODULE._read_regular_source_payload,
            ) as read_payload,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular-file evidence reads.*aggregate.*size limit",
            ),
        ):
            self._plan_initial_actions()

        self.assertEqual(read_payload.call_count, 1)

    def test_exact_noop_does_not_consume_materialization_budget(self) -> None:
        install(self.release, self.home, SHA_A)

        with mock.patch.object(
            MODULE,
            "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
            0,
        ):
            install(self.release, self.home, SHA_A)


class PrivateRegularAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install_public_base(self, *, role_payload: str | None = None) -> None:
        public = self.root / "public"
        write_release(public, role_payload=role_payload)
        install(public, self.home, SHA_A)

    def _install_private(
        self,
        *,
        role_payload: str = 'provider = "private"\n',
        override: bool = False,
    ) -> Path:
        private = self.root / "private"
        write_release(
            private,
            owner="private",
            role_payload=role_payload,
            override=override,
            base_sha=SHA_A,
        )
        install(private, self.home, SHA_B)
        return self.home / ROLE_TARGET

    def test_private_overlay_verify_accepts_healthy_regular_role(self) -> None:
        self._install_public_base()
        target = self._install_private()

        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.verify_overlay(self.home, "private")

    def test_private_overlay_verify_rejects_missing_ledger_claim(self) -> None:
        self._install_public_base()
        self._install_private()
        state = MODULE._load_managed_state(self.home)
        state.links.pop(ROLE_TARGET)
        MODULE._write_managed_state(self.home, state)

        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "overlay verification failed",
            ),
        ):
            MODULE.verify_overlay(self.home, "private")

    def test_private_overlay_verify_rejects_unsafe_regular_target_ancestor(
        self,
    ) -> None:
        self._install_public_base()
        self._install_private()
        os.chmod(self.home, 0o777)
        output = io.StringIO()

        with (
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "overlay verification failed",
            ),
        ):
            MODULE.verify_overlay(self.home, "private")

        self.assertIn(
            "managed regular-file parent access policy mismatch",
            output.getvalue(),
        )

    def test_uninstall_removes_private_only_regular_role(self) -> None:
        self._install_public_base()
        target = self._install_private()

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(os.path.lexists(target))
        self.assertNotIn(ROLE_TARGET, MODULE._load_managed_state(self.home).links)

    def test_uninstall_private_override_restores_public_regular_role(self) -> None:
        public_payload = 'provider = "public"\n'
        self._install_public_base(role_payload=public_payload)
        target = self._install_private(override=True)
        self.assertEqual(target.read_text(encoding="utf-8"), 'provider = "private"\n')

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), public_payload)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)
        record = MODULE._load_managed_state(self.home).links[ROLE_TARGET]
        self.assertEqual(record.owner, MODULE.PUBLIC_OWNER)


class RegularAccessPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.agents = self.home / "agents"
        self.agents.mkdir(mode=0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_planning_rejects_group_writable_regular_parent(self) -> None:
        os.chmod(self.agents, 0o775)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed regular-file parent access policy mismatch",
        ):
            MODULE._capture_reconcile_target_snapshot(
                self.home,
                self.agents / "reviewer.toml",
                require_managed_parent_access=True,
            )

    def test_terminal_snapshot_rejects_world_writable_regular_parent(self) -> None:
        target = self.agents / "reviewer.toml"
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        os.chmod(target, 0o600)
        os.chmod(self.agents, 0o777)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed regular-file parent access policy mismatch",
        ):
            MODULE._read_regular_file_snapshot_beneath(
                self.home,
                target,
                require_managed_access=True,
            )

    def test_parent_access_rejects_foreign_owner_metadata(self) -> None:
        parent_fd = os.open(self.agents, os.O_RDONLY)
        actual = os.fstat(parent_fd)
        foreign = SimpleNamespace(
            st_mode=actual.st_mode,
            st_uid=os.geteuid() + 1,
            st_dev=actual.st_dev,
            st_ino=actual.st_ino,
        )
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_require_release_identity_fd_access_policy",
                    return_value=foreign,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "managed regular-file parent access policy mismatch",
                ),
            ):
                MODULE._require_managed_regular_directory_fd_access(
                    self.home,
                    self.agents,
                    parent_fd,
                )
        finally:
            os.close(parent_fd)

    def test_new_regular_parent_is_revalidated_after_publication(self) -> None:
        target = self.agents / "nested" / "reviewer.toml"
        planned = MODULE._capture_reconcile_target_snapshot(
            self.home,
            target,
            require_managed_parent_access=True,
        )
        real_publish = MODULE._publish_reconcile_directory_noreplace

        def publish_with_unsafe_mode(
            parent_fd: int,
            name: str,
            display_path: Path,
        ) -> tuple[int, tuple[int, int]]:
            directory_fd, identity = real_publish(parent_fd, name, display_path)
            os.fchmod(directory_fd, 0o777)
            return directory_fd, identity

        with (
            mock.patch.object(
                MODULE,
                "_publish_reconcile_directory_noreplace",
                side_effect=publish_with_unsafe_mode,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed regular-file parent access policy mismatch",
            ),
        ):
            MODULE._open_reconcile_parent_for_create(
                self.home,
                target,
                planned,
                {},
                require_managed_parent_access=True,
            )

    def test_destructive_move_rejects_direct_parent_policy_drift(self) -> None:
        target = self.agents / "reviewer.toml"
        backup = self.home / "quarantine" / "reviewer.toml"
        backup.parent.mkdir(mode=0o700)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o600)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )
        os.chmod(self.agents, 0o777)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed regular-file parent access policy mismatch",
        ):
            MODULE._atomic_move_beneath_home(
                self.home,
                target,
                backup,
                planned,
                backup_parent_identity,
            )

        self.assertTrue(target.is_file())
        self.assertFalse(os.path.lexists(backup))

    def test_destructive_move_rejects_ancestor_policy_drift(self) -> None:
        target = self.agents / "nested" / "reviewer.toml"
        backup = self.home / "quarantine" / "reviewer.toml"
        target.parent.mkdir(mode=0o755)
        backup.parent.mkdir(mode=0o700)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o600)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )
        os.chmod(self.agents, 0o777)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed regular-file parent access policy mismatch",
        ):
            MODULE._atomic_move_beneath_home(
                self.home,
                target,
                backup,
                planned,
                backup_parent_identity,
            )

        self.assertTrue(target.is_file())
        self.assertFalse(os.path.lexists(backup))

    def test_destructive_move_tolerates_parent_entry_and_nlink_churn(self) -> None:
        target = self.agents / "reviewer.toml"
        backup = self.home / "quarantine" / "reviewer.toml"
        backup.parent.mkdir(mode=0o700)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o600)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )
        (self.agents / "benign-child").mkdir(mode=0o700)

        MODULE._atomic_move_beneath_home(
            self.home,
            target,
            backup,
            planned,
            backup_parent_identity,
        )

        self.assertFalse(os.path.lexists(target))
        self.assertEqual(
            backup.read_text(encoding="utf-8"),
            'name = "reviewer"\n',
        )

    def test_parent_chain_does_not_treat_directory_nlink_churn_as_mutation(
        self,
    ) -> None:
        parent_fd = os.open(self.agents, os.O_RDONLY)
        sample_count = 0

        def metadata_with_nlink_churn(
            file_descriptor: int,
            _display_path: Path,
            _expected_owner_uid: int,
        ) -> SimpleNamespace:
            nonlocal sample_count
            sample_count += 1
            actual = os.fstat(file_descriptor)
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_uid=actual.st_uid,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
                st_nlink=actual.st_nlink + sample_count,
            )

        try:
            with mock.patch.object(
                MODULE,
                "_require_release_identity_fd_access_policy",
                side_effect=metadata_with_nlink_churn,
            ):
                MODULE._require_managed_regular_parent_chain_access(
                    self.home,
                    self.agents,
                    bound_parent_fd=parent_fd,
                )
        finally:
            os.close(parent_fd)

    def test_darwin_parent_acl_rejects_non_owner_allow(self) -> None:
        parent_fd = os.open(self.agents, os.O_RDONLY)
        owner_uuid = b"o" * MODULE._DARWIN_UUID_BYTES
        foreign_uuid = b"f" * MODULE._DARWIN_UUID_BYTES
        try:
            with (
                mock.patch.object(MODULE.sys, "platform", "darwin"),
                mock.patch.object(
                    MODULE,
                    "_load_darwin_extended_acl_api",
                    return_value=object(),
                ),
                mock.patch.object(
                    MODULE,
                    "_darwin_extended_acl_entries",
                    return_value=((MODULE._DARWIN_ACL_EXTENDED_ALLOW, foreign_uuid),),
                ),
                mock.patch.object(
                    MODULE,
                    "_darwin_owner_uuid",
                    return_value=owner_uuid,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "non-owner qualifier",
                ),
            ):
                MODULE._require_managed_regular_directory_fd_access(
                    self.home,
                    self.agents,
                    parent_fd,
                )
        finally:
            os.close(parent_fd)

    def test_darwin_parent_acl_query_failure_fails_closed(self) -> None:
        parent_fd = os.open(self.agents, os.O_RDONLY)
        try:
            with (
                mock.patch.object(MODULE.sys, "platform", "darwin"),
                mock.patch.object(
                    MODULE,
                    "_load_darwin_extended_acl_api",
                    side_effect=MODULE.SyncError("ACL query unavailable"),
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "access policy cannot be verified.*ACL query unavailable",
                ),
            ):
                MODULE._require_managed_regular_directory_fd_access(
                    self.home,
                    self.agents,
                    parent_fd,
                )
        finally:
            os.close(parent_fd)

    def test_regular_snapshot_revalidates_acl_on_the_same_fd_after_read(
        self,
    ) -> None:
        target = self.agents / "reviewer.toml"
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        os.chmod(target, 0o600)
        parent_fd = os.open(self.agents, os.O_RDONLY)
        sampled_fds: list[int] = []

        def access_policy(
            file_descriptor: int,
            _display_path: Path,
            _expected_owner_uid: int,
        ) -> os.stat_result:
            sampled_fds.append(file_descriptor)
            if len(sampled_fds) == 2:
                raise MODULE.SyncError("later ACL grants non-owner access")
            return os.fstat(file_descriptor)

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_require_release_identity_fd_access_policy",
                    side_effect=access_policy,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "later ACL grants non-owner access",
                ),
            ):
                MODULE._regular_file_snapshot_at(
                    parent_fd,
                    target.name,
                    target,
                )
        finally:
            os.close(parent_fd)
        self.assertEqual(len(sampled_fds), 2)
        self.assertEqual(sampled_fds[0], sampled_fds[1])

    def test_regular_metadata_match_ignores_ctime_only_drift(self) -> None:
        target = self.agents / "reviewer.toml"
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        baseline = os.lstat(target)
        ctime_only = SimpleNamespace(
            st_dev=baseline.st_dev,
            st_ino=baseline.st_ino,
            st_mode=baseline.st_mode,
            st_uid=baseline.st_uid,
            st_gid=baseline.st_gid,
            st_size=baseline.st_size,
            st_nlink=baseline.st_nlink,
            st_ctime_ns=baseline.st_ctime_ns + 1,
        )

        self.assertTrue(MODULE._regular_stat_metadata_matches(ctime_only, baseline))


class RegularAgentPendingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.release = self.root / "release"
        write_release(self.release, role_payload='name = "reviewer"\n')

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_recovered_install(self) -> None:
        target = self.home / ROLE_TARGET
        install(self.release, self.home, SHA_A)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)

    def _interrupt_regular_publication_cleanup(
        self,
        release: Path,
        sha: str,
    ) -> MODULE.PendingLinkBatch:
        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_regular_publication_candidate",
                side_effect=MODULE.SyncError(
                    "injected active publication cleanup crash"
                ),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(release, self.home, sha)

        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertEqual(
            batch.metadata_version,
            MODULE.PENDING_LINK_METADATA_VERSION,
        )
        return batch

    def _assert_active_publication_journal(
        self,
        batch: MODULE.PendingLinkBatch,
    ) -> tuple[MODULE.PendingLinkRecord, Path]:
        record = next(
            candidate
            for candidate in batch.records
            if candidate.is_regular()
            and candidate.action in {"create", "replace", "quarantine-replace"}
        )
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        self.assertIsNotNone(journal)
        assert journal is not None
        journal_snapshot, active_name, _phase, _expected = journal
        self.assertEqual(journal_snapshot.mode, 0o600)
        active = (self.home / Path(*record.target.parts)).with_name(active_name)
        evidence = batch.batch_root / Path(*record.evidence.parts)
        self.assertFalse(os.path.lexists(self.home / Path(*record.target.parts)))
        self.assertEqual(
            (active.stat().st_dev, active.stat().st_ino),
            (evidence.stat().st_dev, evidence.stat().st_ino),
        )
        return record, active

    def _interrupt_uncommitted_regular_publication(
        self,
        release: Path,
        sha: str,
    ) -> MODULE.PendingLinkBatch:
        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_rollback_reconcile_transaction",
                side_effect=MODULE.SyncError("injected hard rollback crash"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(release, self.home, sha)

        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        return batch

    def _downgrade_pending_regular_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        version: int,
    ) -> MODULE.PendingLinkBatch:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        if version == 6:
            payload, _legacy_gid = legacy_v6_writer_metadata_payload(batch)
            assert _legacy_gid is not None
        else:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            payload["version"] = version
            downgrade_pending_state_evidence_metadata(payload, version)
        records = payload["records"]
        assert isinstance(records, list)
        for raw_record in records:
            assert isinstance(raw_record, dict)
            if version < 10:
                raw_record.pop("before_materialization", None)
                raw_record.pop("removed_link", None)
            raw_record.pop("publication_cleanup", None)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, version)
        return parsed

    def _downgrade_durable_pending_regular_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        version: int,
    ) -> MODULE.PendingLinkBatch:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["version"] = version
        downgrade_pending_state_evidence_metadata(payload, version)
        records = payload["records"]
        assert isinstance(records, list)
        for raw_record in records:
            assert isinstance(raw_record, dict)
            if version < 10:
                raw_record.pop("before_materialization", None)
                raw_record.pop("removed_link", None)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, version)
        return parsed

    def _isolate_legacy_regular_publication(
        self,
        batch: MODULE.PendingLinkBatch,
        record: MODULE.PendingLinkRecord,
        target: Path,
    ) -> Path:
        parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
        try:
            parent_identity = MODULE._directory_identity(parent_fd)
            metadata = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            planned = MODULE._pending_cleanup_entry_plan(metadata)
            active_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                parent_identity,
                planned,
            )
            MODULE._rename_noreplace_at(
                parent_fd,
                target.name,
                parent_fd,
                active_name,
            )
            os.fsync(parent_fd)
        finally:
            MODULE._close_fd_quietly(parent_fd)
        active = target.with_name(active_name)
        self.assertFalse(os.path.lexists(target))
        self.assertTrue(active.is_file())
        return active

    def test_regular_to_symlink_precommit_recovery_restores_exact_regular_file(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        original_identity = (target.stat().st_dev, target.stat().st_ino)
        replacement = self.root / "regular-to-symlink-precommit"
        write_regular_to_symlink_release(replacement)

        batch = self._interrupt_uncommitted_regular_publication(
            replacement,
            SHA_B,
        )
        record = next(
            candidate for candidate in batch.records if candidate.target == ROLE_TARGET
        )
        self.assertEqual(batch.metadata_version, 10)
        self.assertTrue(record.before_is_regular())
        self.assertFalse(record.is_regular())
        self.assertEqual(record.action, "quarantine-replace")
        self.assertEqual(
            record.removed_link_key,
            "public:regular-agent-to-symlink",
        )
        self.assertIsNotNone(record.publication_cleanup)
        self.assertTrue(target.is_symlink())

        install(self.release, self.home, SHA_A)

        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            original_identity,
        )
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_same_owner_update_rejects_before_only_removal_authority(self) -> None:
        before_release = self.root / "same-owner-before-only-removal"
        write_release(before_release, role_payload='name = "reviewer"\n')
        before_manifest_path = before_release / MODULE.MANIFEST_RELATIVE_PATH
        before_manifest = json.loads(before_manifest_path.read_text(encoding="utf-8"))
        before_manifest["removed_links"] = [
            {
                "id": "before-only-removal",
                "source": "personal_codex/agents/reviewer.toml",
                "target": ROLE_TARGET.as_posix(),
                "kind": "file",
                "replacement_target": ROLE_TARGET.as_posix(),
            }
        ]
        before_manifest_path.write_text(
            json.dumps(before_manifest) + "\n",
            encoding="utf-8",
        )
        install(before_release, self.home, SHA_A)

        after_release = self.root / "same-owner-after-release"
        write_regular_to_symlink_release(after_release)
        batch = self._interrupt_uncommitted_regular_publication(
            after_release,
            SHA_B,
        )
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        transition = next(
            record
            for record in payload["records"]
            if record.get("target") == ROLE_TARGET.as_posix()
        )
        transition["removed_link"] = "public:before-only-removal"
        write_pending_metadata_payload(batch, payload)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending regular-to-symlink transition lacks exact removal authority",
        ):
            MODULE._load_pending_link_batch(self.home)

        self.assertTrue(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_v10_regular_to_symlink_authority_error_contract(self) -> None:
        install(self.release, self.home, SHA_A)
        replacement = self.root / "regular-to-symlink-operational-error"
        write_regular_to_symlink_release(replacement)
        self._interrupt_uncommitted_regular_publication(replacement, SHA_B)

        with (
            mock.patch.object(
                MODULE,
                "_pending_release_removal_authority",
                side_effect=OSError("injected unreadable receipt release"),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "cannot revalidate pending regular-to-symlink removal authority",
            ),
        ):
            MODULE._load_pending_link_batch(self.home)

        semantic_error = MODULE.SyncError("injected semantic release failure")
        with (
            mock.patch.object(
                MODULE,
                "_pending_release_removal_authority",
                side_effect=semantic_error,
            ),
            self.assertRaises(MODULE.SyncError) as raised,
        ):
            MODULE._load_pending_link_batch(self.home)
        self.assertIs(raised.exception, semantic_error)

    def test_v8_v9_managed_regular_remove_accepts_null_publication_cleanup(
        self,
    ) -> None:
        for version in (8, 9):
            with self.subTest(version=version):
                self.home = self.root / f"home-v{version}-null-cleanup"
                install(self.release, self.home, SHA_A)
                removal_release = self.root / f"release-v{version}-remove"
                write_release(removal_release)
                batch = self._interrupt_uncommitted_regular_publication(
                    removal_release,
                    SHA_B,
                )
                metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                payload["version"] = version
                downgrade_pending_state_evidence_metadata(payload, version)
                for raw_record in payload["records"]:
                    raw_record.pop("before_materialization")
                    raw_record.pop("removed_link")
                removal_record = next(
                    raw_record
                    for raw_record in payload["records"]
                    if raw_record["target"] == ROLE_TARGET.as_posix()
                )
                removal_record["publication_cleanup"] = None
                write_pending_metadata_payload(batch, payload)

                parsed = MODULE._load_pending_link_batch(self.home)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.metadata_version, version)

    def test_v10_managed_regular_remove_rejects_null_publication_cleanup(self) -> None:
        install(self.release, self.home, SHA_A)
        removal_release = self.root / "release-v10-remove-null-cleanup"
        write_release(removal_release)
        batch = self._interrupt_uncommitted_regular_publication(
            removal_release,
            SHA_B,
        )
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        removal_record = next(
            raw_record
            for raw_record in payload["records"]
            if raw_record["target"] == ROLE_TARGET.as_posix()
        )
        removal_record["publication_cleanup"] = None
        write_pending_metadata_payload(batch, payload)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "invalid publication cleanup path",
        ):
            MODULE._load_pending_link_batch(self.home)

    def test_create_rollback_recovers_durable_active_publication_cleanup(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        _record, active = self._assert_active_publication_journal(batch)
        self.assertGreater(active.stat().st_nlink, 2)

        self._assert_recovered_install()
        self.assertFalse(os.path.lexists(active))
        cleanup_path = MODULE._pending_regular_publication_cleanup_path(
            batch,
            next(record for record in batch.records if record.is_regular()),
            "produced",
        )
        self.assertTrue(cleanup_path.is_file())

    def test_private_deleted_authority_does_not_reauthorize_relinked_public_name(
        self,
    ) -> None:
        for public_name in ("canonical", "active"):
            with self.subTest(public_name=public_name):
                self.home = self.root / f"home-private-deleted-{public_name}"
                batch = self._interrupt_regular_publication_cleanup(
                    self.release,
                    SHA_A,
                )
                record, active = self._assert_active_publication_journal(batch)
                target = self.home / Path(*record.target.parts)
                assert record.evidence is not None
                evidence = batch.batch_root / Path(*record.evidence.parts)

                MODULE._recover_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )

                journal = MODULE._read_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
                assert journal is not None
                self.assertEqual(
                    MODULE._pending_regular_publication_cleanup_lifecycle(journal[0]),
                    "public-authorized",
                )
                self.assertTrue(
                    MODULE._pending_regular_publication_private_authority_path(
                        batch,
                        record,
                        "produced",
                    ).is_file()
                )
                self.assertFalse(os.path.lexists(target))
                self.assertFalse(os.path.lexists(active))

                restarted = MODULE._load_pending_link_batch(self.home)
                self.assertIsNotNone(restarted)
                assert restarted is not None
                restarted_record = next(
                    candidate
                    for candidate in restarted.records
                    if candidate.index == record.index
                )
                public = target if public_name == "canonical" else active
                os.link(evidence, public, follow_symlinks=False)
                public_identity = (public.stat().st_dev, public.stat().st_ino)

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "public cleanup authority was revoked",
                ):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        restarted,
                        restarted_record,
                        "produced",
                    )

                self.assertTrue(public.is_file())
                self.assertEqual(
                    (public.stat().st_dev, public.stat().st_ino),
                    public_identity,
                )
                self.assertEqual(public.read_bytes(), evidence.read_bytes())

    def test_private_unlink_failure_retries_from_private_authority(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, active = self._assert_active_publication_journal(batch)
        target = self.home / Path(*record.target.parts)
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        _snapshot, _active_name, _phase, expected = journal
        cleanup = batch.batch_root / "pending" / "cleanup"
        cleanup_metadata = cleanup.stat()
        cleanup_identity = (cleanup_metadata.st_dev, cleanup_metadata.st_ino)
        private_name = MODULE._pending_regular_publication_private_alias_name(
            cleanup_identity,
            (*expected.file_identity, stat.S_IFREG),
            record.index,
            "produced",
        )
        private = cleanup / private_name
        real_unlink = MODULE.os.unlink
        failed = False

        def fail_first_private_unlink(
            name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal failed
            if (
                MODULE._pending_regular_publication_private_deletion_alias_base(
                    os.fsdecode(name)
                )
                == private_name
                and not failed
            ):
                failed = True
                raise MODULE.SyncError("injected private unlink failure")
            real_unlink(name, *args, **kwargs)

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=fail_first_private_unlink,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "private unlink failure"),
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertTrue(failed)
        self.assertFalse(os.path.lexists(private))
        private_binding = MODULE._pending_regular_publication_private_alias_binding(
            self.home,
            batch,
            record,
            "produced",
            expected.file_identity,
        )
        self.assertIsNotNone(private_binding)
        assert private_binding is not None
        self.assertTrue(private_binding[0].startswith(private_name + ".delete-"))
        self.assertFalse(os.path.lexists(target))
        self.assertFalse(os.path.lexists(active))
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        self.assertEqual(
            MODULE._pending_regular_publication_cleanup_lifecycle(journal[0]),
            "public-authorized",
        )
        self.assertTrue(
            MODULE._pending_regular_publication_private_authority_path(
                batch,
                record,
                "produced",
            ).is_file()
        )

        restarted = MODULE._load_pending_link_batch(self.home)
        assert restarted is not None
        restarted_record = next(
            candidate
            for candidate in restarted.records
            if candidate.index == record.index
        )
        MODULE._recover_pending_regular_publication_cleanup(
            self.home,
            restarted,
            restarted_record,
            "produced",
        )
        self.assertFalse(os.path.lexists(private))
        self.assertFalse(os.path.lexists(target))
        self.assertFalse(os.path.lexists(active))

    def test_private_authority_retains_original_journal_identity_and_payload(
        self,
    ) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, _active = self._assert_active_publication_journal(batch)
        journal_path = MODULE._pending_regular_publication_cleanup_path(
            batch,
            record,
            "produced",
        )
        journal_before = journal_path.stat()
        payload_before = journal_path.read_bytes()

        MODULE._recover_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )

        journal_after = journal_path.stat()
        self.assertEqual(
            (journal_after.st_dev, journal_after.st_ino),
            (journal_before.st_dev, journal_before.st_ino),
        )
        self.assertEqual(journal_path.read_bytes(), payload_before)
        self.assertEqual(stat.S_IMODE(journal_after.st_mode), 0o600)
        self.assertTrue(
            MODULE._pending_regular_publication_private_authority_path(
                batch,
                record,
                "produced",
            ).is_file()
        )

    def test_final_private_isolation_rename_crash_recovers_on_restart(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, _active = self._assert_active_publication_journal(batch)
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        expected = journal[3]
        cleanup = batch.batch_root / "pending" / "cleanup"
        cleanup_stat = cleanup.stat()
        private_name = MODULE._pending_regular_publication_private_alias_name(
            (cleanup_stat.st_dev, cleanup_stat.st_ino),
            (*expected.file_identity, stat.S_IFREG),
            record.index,
            "produced",
        )
        real_rename = MODULE._rename_noreplace_at
        crashed = False

        def crash_after_final_private_isolation(
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
                source_name == private_name
                and MODULE._pending_regular_publication_private_deletion_alias_base(
                    destination_name
                )
                == private_name
            ):
                crashed = True
                raise MODULE.SyncError("injected final private isolation crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_final_private_isolation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "final private isolation crash",
            ),
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertTrue(crashed)
        self.assertFalse(os.path.lexists(cleanup / private_name))
        private_binding = MODULE._pending_regular_publication_private_alias_binding(
            self.home,
            batch,
            record,
            "produced",
            expected.file_identity,
        )
        self.assertIsNotNone(private_binding)
        assert private_binding is not None
        self.assertTrue(private_binding[0].startswith(private_name + ".delete-"))

        restarted = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(restarted)
        assert restarted is not None
        restarted_record = next(
            candidate
            for candidate in restarted.records
            if candidate.index == record.index
        )
        MODULE._recover_pending_regular_publication_cleanup(
            self.home,
            restarted,
            restarted_record,
            "produced",
        )
        self.assertIsNone(
            MODULE._pending_regular_publication_private_alias_binding(
                self.home,
                restarted,
                restarted_record,
                "produced",
                expected.file_identity,
            )
        )

    def test_final_private_isolation_preserves_replacement_and_same_inode_drift(
        self,
    ) -> None:
        for drift in ("replacement", "content", "policy"):
            with self.subTest(drift=drift):
                self.home = self.root / f"home-final-private-{drift}"
                batch = self._interrupt_regular_publication_cleanup(
                    self.release,
                    SHA_A,
                )
                record, _active = self._assert_active_publication_journal(batch)
                journal = MODULE._read_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
                assert journal is not None
                expected = journal[3]
                cleanup = batch.batch_root / "pending" / "cleanup"
                cleanup_stat = cleanup.stat()
                private_name = MODULE._pending_regular_publication_private_alias_name(
                    (cleanup_stat.st_dev, cleanup_stat.st_ino),
                    (*expected.file_identity, stat.S_IFREG),
                    record.index,
                    "produced",
                )
                real_rename = MODULE._rename_noreplace_at
                drifted = False

                def drift_after_final_private_isolation(
                    source_fd: int,
                    source_name: str,
                    destination_fd: int,
                    destination_name: str,
                ) -> None:
                    nonlocal drifted
                    real_rename(
                        source_fd,
                        source_name,
                        destination_fd,
                        destination_name,
                    )
                    if (
                        source_name != private_name
                        or MODULE._pending_regular_publication_private_deletion_alias_base(
                            destination_name
                        )
                        != private_name
                    ):
                        return
                    if drift == "replacement":
                        os.unlink(destination_name, dir_fd=destination_fd)
                        file_fd = os.open(
                            destination_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=destination_fd,
                        )
                        try:
                            os.write(file_fd, b"foreign\n")
                        finally:
                            os.close(file_fd)
                    elif drift == "content":
                        file_fd = os.open(
                            destination_name,
                            os.O_WRONLY | os.O_TRUNC,
                            dir_fd=destination_fd,
                        )
                        try:
                            os.write(file_fd, b"foreign\n")
                        finally:
                            os.close(file_fd)
                    else:
                        os.chmod(
                            destination_name,
                            0o640,
                            dir_fd=destination_fd,
                            follow_symlinks=False,
                        )
                    drifted = True

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_noreplace_at",
                        side_effect=drift_after_final_private_isolation,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "changed during final private isolation",
                    ),
                ):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        batch,
                        record,
                        "produced",
                    )

                self.assertTrue(drifted)
                self.assertFalse(os.path.lexists(cleanup / private_name))
                retained = tuple(
                    child
                    for child in cleanup.iterdir()
                    if child.name.startswith(
                        MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX
                    )
                )
                self.assertEqual(len(retained), 1)
                retained_stat = retained[0].stat()
                retained_identity = (retained_stat.st_dev, retained_stat.st_ino)
                if drift == "replacement":
                    self.assertNotEqual(retained_identity, expected.file_identity)
                    self.assertEqual(retained[0].read_bytes(), b"foreign\n")
                else:
                    self.assertEqual(retained_identity, expected.file_identity)

    def test_final_private_deletion_rebinds_after_parent_policy_revalidation(
        self,
    ) -> None:
        for drift in ("replacement", "symlink", "content", "policy"):
            with self.subTest(drift=drift):
                self.home = self.root / f"home-final-delete-{drift}"
                batch = self._interrupt_regular_publication_cleanup(
                    self.release,
                    SHA_A,
                )
                record, _active = self._assert_active_publication_journal(batch)
                journal = MODULE._read_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
                assert journal is not None
                expected = journal[3]
                cleanup = batch.batch_root / "pending" / "cleanup"
                real_require_policy = MODULE._require_pending_cleanup_fd_access_policy
                drifted_name: str | None = None

                def drift_after_final_parent_policy(
                    file_descriptor: int,
                    display_path: Path,
                    *,
                    expected_mode: int,
                ) -> os.stat_result:
                    nonlocal drifted_name
                    result = real_require_policy(
                        file_descriptor,
                        display_path,
                        expected_mode=expected_mode,
                    )
                    if drifted_name is not None or display_path != cleanup:
                        return result
                    deletion_names = tuple(
                        name
                        for name in os.listdir(file_descriptor)
                        if MODULE._pending_regular_publication_private_deletion_alias_base(
                            name
                        )
                        is not None
                    )
                    if len(deletion_names) != 1:
                        return result
                    drifted_name = deletion_names[0]
                    if drift in {"replacement", "symlink"}:
                        os.unlink(drifted_name, dir_fd=file_descriptor)
                    if drift == "replacement":
                        replacement_fd = os.open(
                            drifted_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=file_descriptor,
                        )
                        try:
                            os.write(replacement_fd, b"foreign replacement\n")
                        finally:
                            os.close(replacement_fd)
                    elif drift == "symlink":
                        os.symlink(
                            "foreign-target",
                            drifted_name,
                            dir_fd=file_descriptor,
                        )
                    elif drift == "content":
                        content_fd = os.open(
                            drifted_name,
                            os.O_WRONLY | os.O_TRUNC,
                            dir_fd=file_descriptor,
                        )
                        try:
                            os.write(content_fd, b"foreign content\n")
                        finally:
                            os.close(content_fd)
                    else:
                        os.chmod(
                            drifted_name,
                            0o640,
                            dir_fd=file_descriptor,
                            follow_symlinks=False,
                        )
                    return result

                with (
                    mock.patch.object(
                        MODULE,
                        "_require_pending_cleanup_fd_access_policy",
                        side_effect=drift_after_final_parent_policy,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "final private evidence changed after boundary revalidation",
                    ),
                ):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        batch,
                        record,
                        "produced",
                    )

                self.assertIsNotNone(drifted_name)
                assert drifted_name is not None
                drifted_path = cleanup / drifted_name
                self.assertTrue(os.path.lexists(drifted_path))
                if drift == "replacement":
                    self.assertNotEqual(
                        (drifted_path.stat().st_dev, drifted_path.stat().st_ino),
                        expected.file_identity,
                    )
                    self.assertEqual(
                        drifted_path.read_bytes(),
                        b"foreign replacement\n",
                    )
                elif drift == "symlink":
                    self.assertTrue(drifted_path.is_symlink())
                    self.assertEqual(os.readlink(drifted_path), "foreign-target")
                elif drift == "content":
                    self.assertEqual(
                        (drifted_path.stat().st_dev, drifted_path.stat().st_ino),
                        expected.file_identity,
                    )
                    self.assertEqual(drifted_path.read_bytes(), b"foreign content\n")
                else:
                    self.assertEqual(
                        (drifted_path.stat().st_dev, drifted_path.stat().st_ino),
                        expected.file_identity,
                    )
                    self.assertEqual(stat.S_IMODE(drifted_path.stat().st_mode), 0o640)

    def test_private_authority_anchor_rejects_v2_journal_replay(self) -> None:
        for public_name in ("canonical", "active"):
            with self.subTest(public_name=public_name):
                self.home = self.root / f"home-v2-replay-{public_name}"
                batch = self._interrupt_regular_publication_cleanup(
                    self.release,
                    SHA_A,
                )
                record, active = self._assert_active_publication_journal(batch)
                target = self.home / Path(*record.target.parts)
                assert record.evidence is not None
                evidence = batch.batch_root / Path(*record.evidence.parts)
                journal_path = MODULE._pending_regular_publication_cleanup_path(
                    batch,
                    record,
                    "produced",
                )
                saved_v2 = journal_path.read_bytes()

                MODULE._recover_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
                anchor_path = (
                    MODULE._pending_regular_publication_private_authority_path(
                        batch,
                        record,
                        "produced",
                    )
                )
                self.assertTrue(anchor_path.is_file())
                journal_path.write_bytes(saved_v2)
                public = target if public_name == "canonical" else active
                os.link(evidence, public, follow_symlinks=False)
                public_identity = (public.stat().st_dev, public.stat().st_ino)

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "public cleanup authority was revoked",
                ):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        batch,
                        record,
                        "produced",
                    )

                self.assertEqual(
                    (public.stat().st_dev, public.stat().st_ino),
                    public_identity,
                )
                self.assertTrue(anchor_path.is_file())

    def test_v1_cleanup_journal_missing_lifecycle_fails_closed(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, active = self._assert_active_publication_journal(batch)
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        _snapshot, active_name, _phase, expected = journal
        journal_path = MODULE._pending_regular_publication_cleanup_path(
            batch,
            record,
            "produced",
        )
        journal_path.write_bytes(
            MODULE._pending_regular_publication_cleanup_payload(
                batch,
                record,
                expected,
                "produced",
                active_name,
                version=1,
            )
        )
        active_identity = (active.stat().st_dev, active.stat().st_ino)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "public cleanup authority was revoked",
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertEqual(
            (active.stat().st_dev, active.stat().st_ino),
            active_identity,
        )

    def test_cleanup_journal_unhashable_version_fails_closed(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, active = self._assert_active_publication_journal(batch)
        journal_path = MODULE._pending_regular_publication_cleanup_path(
            batch,
            record,
            "produced",
        )
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        payload["version"] = []
        journal_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        active_identity = (active.stat().st_dev, active.stat().st_ino)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "cleanup journal changed",
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertEqual(
            (active.stat().st_dev, active.stat().st_ino),
            active_identity,
        )

    def test_v3_cleanup_journal_unhashable_phase_fails_closed(self) -> None:
        for malformed_phase in ([], {}):
            with self.subTest(malformed_phase=malformed_phase):
                self.home = self.root / (
                    "home-v3-unhashable-phase-" + type(malformed_phase).__name__
                )
                batch = self._interrupt_regular_publication_cleanup(
                    self.release,
                    SHA_A,
                )
                record, active = self._assert_active_publication_journal(batch)
                journal = MODULE._read_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
                assert journal is not None
                _snapshot, active_name, _phase, expected = journal
                journal_path = MODULE._pending_regular_publication_cleanup_path(
                    batch,
                    record,
                    "produced",
                )
                cleanup_parent = journal_path.parent.stat()
                payload = json.loads(
                    MODULE._pending_regular_publication_cleanup_payload(
                        batch,
                        record,
                        expected,
                        "produced",
                        active_name,
                        version=3,
                        cleanup_parent_identity=(
                            cleanup_parent.st_dev,
                            cleanup_parent.st_ino,
                        ),
                    )
                )
                payload["phase"] = malformed_phase
                journal_path.write_text(
                    json.dumps(payload) + "\n",
                    encoding="utf-8",
                )
                active_identity = (active.stat().st_dev, active.stat().st_ino)

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending regular publication cleanup journal changed",
                ):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        batch,
                        record,
                        "produced",
                    )

                self.assertEqual(
                    (active.stat().st_dev, active.stat().st_ino),
                    active_identity,
                )

    def test_private_authority_journal_corruption_preserves_public_name(self) -> None:
        for corruption in ("missing-lifecycle", "invalid-json"):
            with self.subTest(corruption=corruption):
                self.home = self.root / f"home-private-journal-{corruption}"
                batch = self._interrupt_regular_publication_cleanup(
                    self.release,
                    SHA_A,
                )
                record, _active = self._assert_active_publication_journal(batch)
                target = self.home / Path(*record.target.parts)
                assert record.evidence is not None
                evidence = batch.batch_root / Path(*record.evidence.parts)
                MODULE._recover_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
                os.link(evidence, target, follow_symlinks=False)
                target_identity = (target.stat().st_dev, target.stat().st_ino)
                journal_path = MODULE._pending_regular_publication_cleanup_path(
                    batch,
                    record,
                    "produced",
                )
                if corruption == "missing-lifecycle":
                    payload = json.loads(journal_path.read_text(encoding="utf-8"))
                    payload.pop("lifecycle")
                    journal_path.write_text(
                        json.dumps(payload) + "\n",
                        encoding="utf-8",
                    )
                else:
                    journal_path.write_bytes(b"{")

                with self.assertRaises(MODULE.SyncError):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        batch,
                        record,
                        "produced",
                    )

                self.assertEqual(
                    (target.stat().st_dev, target.stat().st_ino),
                    target_identity,
                )

    def test_durable_private_cleanup_retains_authority_when_canonical_reappears(
        self,
    ) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, active = self._assert_active_publication_journal(batch)
        target = self.home / Path(*record.target.parts)
        real_rename_noreplace = MODULE._rename_noreplace_at
        replacement_identity: tuple[int, int] | None = None
        reappeared = False

        def recreate_canonical_after_private_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal reappeared, replacement_identity
            real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                source_name != active.name
                or source_parent_fd == destination_parent_fd
                or reappeared
            ):
                return
            replacement_fd = os.open(
                target.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=source_parent_fd,
            )
            try:
                os.write(replacement_fd, b"foreign")
                replacement = os.fstat(replacement_fd)
                replacement_identity = (replacement.st_dev, replacement.st_ino)
            finally:
                os.close(replacement_fd)
            reappeared = True

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=recreate_canonical_after_private_isolation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "public name reappeared; exact private evidence was retained",
            ),
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertTrue(reappeared)
        self.assertIsNotNone(replacement_identity)
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            replacement_identity,
        )
        cleanup = batch.batch_root / "pending" / "cleanup"
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        expected = journal[3]
        cleanup_metadata = cleanup.stat()
        private = cleanup / MODULE._pending_regular_publication_private_alias_name(
            (cleanup_metadata.st_dev, cleanup_metadata.st_ino),
            (*expected.file_identity, stat.S_IFREG),
            record.index,
            "produced",
        )
        self.assertEqual(private.read_bytes(), b'name = "reviewer"\n')
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_durable_private_cleanup_reports_canonical_reappearance_after_unlink(
        self,
    ) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, _active = self._assert_active_publication_journal(batch)
        target = self.home / Path(*record.target.parts)
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        _snapshot, _active_name, _phase, expected = journal
        cleanup = batch.batch_root / "pending" / "cleanup"
        cleanup_stat = cleanup.stat()
        private_name = MODULE._pending_regular_publication_private_alias_name(
            (cleanup_stat.st_dev, cleanup_stat.st_ino),
            (expected.file_identity[0], expected.file_identity[1], stat.S_IFREG),
            record.index,
            "produced",
        )
        real_unlink = MODULE.os.unlink
        replacement_identity: tuple[int, int] | None = None
        reappeared = False

        def recreate_canonical_before_private_unlink(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal reappeared, replacement_identity
            if (
                MODULE._pending_regular_publication_private_deletion_alias_base(name)
                == private_name
                and not reappeared
            ):
                target.write_bytes(b"foreign")
                target.chmod(0o600)
                replacement = target.stat()
                replacement_identity = (replacement.st_dev, replacement.st_ino)
                reappeared = True
            real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=recreate_canonical_before_private_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "public name reappeared while private cleanup completed",
            ),
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertTrue(reappeared)
        self.assertIsNotNone(replacement_identity)
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            replacement_identity,
        )
        self.assertFalse(os.path.lexists(cleanup / private_name))
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self.assertTrue(
            MODULE._pending_regular_publication_cleanup_path(
                batch,
                record,
                "produced",
            ).is_file()
        )

    def test_durable_private_cleanup_retains_private_reappearance_after_unlink(
        self,
    ) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        record, active = self._assert_active_publication_journal(batch)
        target = self.home / Path(*record.target.parts)
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        _snapshot, _active_name, _phase, expected = journal
        cleanup = batch.batch_root / "pending" / "cleanup"
        cleanup_stat = cleanup.stat()
        private_name = MODULE._pending_regular_publication_private_alias_name(
            (cleanup_stat.st_dev, cleanup_stat.st_ino),
            (expected.file_identity[0], expected.file_identity[1], stat.S_IFREG),
            record.index,
            "produced",
        )
        real_unlink = MODULE.os.unlink
        replacement_identity: tuple[int, int] | None = None
        reappeared = False

        def recreate_private_after_unlink(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal reappeared, replacement_identity
            real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]
            if (
                MODULE._pending_regular_publication_private_deletion_alias_base(name)
                != private_name
                or reappeared
            ):
                return
            directory_fd = kwargs.get("dir_fd")
            assert isinstance(directory_fd, int)
            replacement_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(replacement_fd, b"foreign")
                replacement = os.fstat(replacement_fd)
                replacement_identity = (replacement.st_dev, replacement.st_ino)
            finally:
                os.close(replacement_fd)
            reappeared = True

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=recreate_private_after_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "private name reappeared after deletion",
            ),
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertTrue(reappeared)
        self.assertIsNotNone(replacement_identity)
        self.assertFalse(os.path.lexists(target))
        self.assertFalse(os.path.lexists(active))
        retained = tuple(
            child
            for child in cleanup.iterdir()
            if MODULE._pending_regular_publication_private_deletion_alias_base(
                child.name
            )
            == private_name
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"foreign")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            replacement_identity,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_v8_v9_v10_produced_private_alias_accepts_exact_preimage(
        self,
    ) -> None:
        for version in (8, 9, MODULE.PENDING_LINK_METADATA_VERSION):
            with self.subTest(version=version):
                self.home = self.root / f"home-v{version}-durable-private-preimage"
                install(self.release, self.home, SHA_A)
                target = self.home / ROLE_TARGET
                old_identity = (target.stat().st_dev, target.stat().st_ino)
                next_release = self.root / f"release-v{version}-durable-private"
                write_release(next_release, role_payload='name = "updated"\n')
                batch = self._interrupt_regular_publication_cleanup(
                    next_release,
                    SHA_B,
                )
                if version != MODULE.PENDING_LINK_METADATA_VERSION:
                    batch = self._downgrade_durable_pending_regular_metadata(
                        batch,
                        version,
                    )
                record, active = self._assert_active_publication_journal(batch)
                journal = MODULE._read_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
                assert journal is not None
                _snapshot, _active_name, _phase, expected = journal
                real_rename = MODULE._rename_noreplace_at

                def crash_after_private_rename(
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
                    if source_name == active.name and source_fd != destination_fd:
                        raise MODULE.SyncError("injected durable private rename crash")

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_noreplace_at",
                        side_effect=crash_after_private_rename,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "durable private rename crash",
                    ),
                ):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        batch,
                        record,
                        "produced",
                    )

                cleanup = batch.batch_root / "pending" / "cleanup"
                cleanup_metadata = cleanup.stat()
                private_name = MODULE._pending_regular_publication_private_alias_name(
                    (cleanup_metadata.st_dev, cleanup_metadata.st_ino),
                    (*expected.file_identity, stat.S_IFREG),
                    record.index,
                    "produced",
                )
                private = cleanup / private_name
                self.assertTrue(private.is_file())
                self.assertFalse(os.path.lexists(active))
                assert record.before_evidence is not None
                before = batch.batch_root / Path(*record.before_evidence.parts)
                os.link(before, target, follow_symlinks=False)
                self.assertEqual(
                    (target.stat().st_dev, target.stat().st_ino),
                    old_identity,
                )

                MODULE._recover_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )

                self.assertFalse(os.path.lexists(private))
                self.assertEqual(
                    (target.stat().st_dev, target.stat().st_ino),
                    old_identity,
                )
                install(self.release, self.home, SHA_A)
                self.assertFalse(batch.batch_root.exists())
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                )
                self.assertEqual(
                    (target.stat().st_dev, target.stat().st_ino),
                    old_identity,
                )
                self.assertEqual(target.stat().st_nlink, 1)

    def test_replace_produced_private_alias_rejects_foreign_canonical_races(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        next_release = self.root / "next-release-private-canonical-races"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        record, active = self._assert_active_publication_journal(batch)
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        _snapshot, _active_name, _phase, expected = journal
        real_rename = MODULE._rename_noreplace_at

        def crash_after_private_rename(
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
            if source_name == active.name and source_fd != destination_fd:
                raise MODULE.SyncError("injected durable private rename crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_private_rename,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "durable private rename crash",
            ),
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        cleanup = batch.batch_root / "pending" / "cleanup"
        cleanup_metadata = cleanup.stat()
        private_name = MODULE._pending_regular_publication_private_alias_name(
            (cleanup_metadata.st_dev, cleanup_metadata.st_ino),
            (*expected.file_identity, stat.S_IFREG),
            record.index,
            "produced",
        )
        private = cleanup / private_name
        target.write_text("foreign before unlink\n", encoding="utf-8")
        target.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "public name reappeared; exact private evidence was retained",
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertTrue(private.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "foreign before unlink\n")
        target.unlink()
        assert record.before_evidence is not None
        before = batch.batch_root / Path(*record.before_evidence.parts)
        os.link(before, target, follow_symlinks=False)
        real_unlink = MODULE.os.unlink
        replaced = False

        def replace_canonical_at_private_unlink(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            if (
                MODULE._pending_regular_publication_private_deletion_alias_base(name)
                == private_name
                and not replaced
            ):
                real_unlink(target)
                target.write_text("foreign after validation\n", encoding="utf-8")
                target.chmod(0o600)
                replaced = True
            real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=replace_canonical_at_private_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "public name reappeared while private cleanup completed",
            ),
        ):
            MODULE._recover_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )

        self.assertTrue(replaced)
        self.assertFalse(os.path.lexists(private))
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "foreign after validation\n",
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_replace_rollback_recovers_durable_active_publication_cleanup(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')

        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        _record, active = self._assert_active_publication_journal(batch)

        install(self.release, self.home, SHA_A)

        self.assertFalse(os.path.lexists(active))
        self.assertFalse(batch.batch_root.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_replace_recovery_accepts_receipt_after_preimage_restoration(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        _record, active = self._assert_active_publication_journal(batch)
        real_restore = MODULE._restore_pending_record_before
        restored = False

        def fail_after_preimage_restoration(
            home: Path,
            pending_batch: MODULE.PendingLinkBatch,
            pending_record: MODULE.PendingLinkRecord,
            before_evidence: MODULE.SymlinkSnapshot | MODULE.RegularFileSnapshot,
        ) -> None:
            nonlocal restored
            real_restore(home, pending_batch, pending_record, before_evidence)
            restored = True
            raise MODULE.SyncError("injected crash after preimage restoration")

        with (
            mock.patch.object(
                MODULE,
                "_restore_pending_record_before",
                side_effect=fail_after_preimage_restoration,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected crash after preimage restoration",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(restored)
        self.assertFalse(os.path.lexists(active))
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertTrue(
            MODULE._pending_regular_publication_cleanup_path(
                batch,
                record,
                "produced",
            ).is_file()
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

        install(self.release, self.home, SHA_A)

        self.assertFalse(MODULE._pending_link_pointer_path(self.home).exists())
        self.assertFalse(batch.batch_root.exists())
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_replace_recovery_recovers_interrupted_before_publication_cleanup(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        source = batch.batch_root / Path(*record.before_evidence.parts)
        real_bound = MODULE._bound_directory_matches
        real_isolate = MODULE._isolate_and_delete_pending_regular_publication_candidate
        before_publish_started = False

        def fail_after_before_publication(
            home: Path,
            path: Path,
            directory_fd: int,
        ) -> bool:
            nonlocal before_publish_started
            if path == source.parent and os.path.lexists(target):
                before_publish_started = True
                return False
            return real_bound(home, path, directory_fd)

        def fail_before_private_isolation(
            home: Path,
            pending_batch: MODULE.PendingLinkBatch,
            pending_record: MODULE.PendingLinkRecord,
            cleanup_phase: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            if before_publish_started and cleanup_phase == "before":
                raise MODULE.SyncError("injected before publication cleanup crash")
            real_isolate(
                home,
                pending_batch,
                pending_record,
                cleanup_phase,
                *args,
                **kwargs,
            )

        with (
            mock.patch.object(
                MODULE,
                "_bound_directory_matches",
                side_effect=fail_after_before_publication,
            ),
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_regular_publication_candidate",
                side_effect=fail_before_private_isolation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exact cleanup could not be verified",
            ),
        ):
            install(self.release, self.home, SHA_A)

        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "before",
        )
        self.assertIsNotNone(journal)
        assert journal is not None
        _snapshot, active_name, phase, _expected = journal
        self.assertEqual(phase, "before")
        active = target.with_name(active_name)
        self.assertEqual((active.stat().st_dev, active.stat().st_ino), old_identity)

        real_restore = MODULE._restore_pending_record_before
        restored = False

        def crash_after_preimage_restoration(
            home: Path,
            pending_batch: MODULE.PendingLinkBatch,
            pending_record: MODULE.PendingLinkRecord,
            before_evidence: MODULE.SymlinkSnapshot | MODULE.RegularFileSnapshot,
        ) -> None:
            nonlocal restored
            real_restore(home, pending_batch, pending_record, before_evidence)
            restored = True
            raise MODULE.SyncError("injected crash after before-state restoration")

        with (
            mock.patch.object(
                MODULE,
                "_restore_pending_record_before",
                side_effect=crash_after_preimage_restoration,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected crash after before-state restoration",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(restored)
        self.assertFalse(os.path.lexists(active))
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "before",
        )
        self.assertIsNotNone(journal)
        assert journal is not None
        self.assertEqual(target.stat().st_nlink, journal[3].link_count)
        cleanup_metadata = journal[0]
        assert cleanup_metadata.parent_identity is not None
        anchor = MODULE._read_pending_regular_publication_private_authority(
            self.home,
            batch,
            record,
            "before",
            journal[3],
            journal[1],
            cleanup_metadata.parent_identity,
        )
        self.assertIsNotNone(anchor)

        install(self.release, self.home, SHA_A)

        self.assertFalse(MODULE._pending_link_pointer_path(self.home).exists())
        self.assertFalse(batch.batch_root.exists())
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_replace_recovery_restores_preimage_after_before_cleanup_crash(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        source = batch.batch_root / Path(*record.before_evidence.parts)
        real_bound = MODULE._bound_directory_matches
        real_isolate = MODULE._isolate_and_delete_pending_regular_publication_candidate
        before_publish_started = False

        def fail_after_before_publication(
            home: Path,
            path: Path,
            directory_fd: int,
        ) -> bool:
            nonlocal before_publish_started
            if path == source.parent and os.path.lexists(target):
                before_publish_started = True
                return False
            return real_bound(home, path, directory_fd)

        def fail_before_private_isolation(
            home: Path,
            pending_batch: MODULE.PendingLinkBatch,
            pending_record: MODULE.PendingLinkRecord,
            cleanup_phase: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            if before_publish_started and cleanup_phase == "before":
                raise MODULE.SyncError("injected before publication cleanup crash")
            real_isolate(
                home,
                pending_batch,
                pending_record,
                cleanup_phase,
                *args,
                **kwargs,
            )

        with (
            mock.patch.object(
                MODULE,
                "_bound_directory_matches",
                side_effect=fail_after_before_publication,
            ),
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_regular_publication_candidate",
                side_effect=fail_before_private_isolation,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exact cleanup could not be verified",
            ),
        ):
            install(self.release, self.home, SHA_A)

        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "before",
        )
        self.assertIsNotNone(journal)
        assert journal is not None
        _snapshot, active_name, phase, _expected = journal
        self.assertEqual(phase, "before")
        active = target.with_name(active_name)
        self.assertEqual((active.stat().st_dev, active.stat().st_ino), old_identity)

        restore_started = False

        def crash_before_preimage_restoration(
            home: Path,
            pending_batch: MODULE.PendingLinkBatch,
            pending_record: MODULE.PendingLinkRecord,
            before_evidence: MODULE.SymlinkSnapshot | MODULE.RegularFileSnapshot,
        ) -> None:
            nonlocal restore_started
            restore_started = True
            self.assertFalse(os.path.lexists(target))
            self.assertFalse(os.path.lexists(active))
            raise MODULE.SyncError("injected crash before before-state restoration")

        with (
            mock.patch.object(
                MODULE,
                "_restore_pending_record_before",
                side_effect=crash_before_preimage_restoration,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected crash before before-state restoration",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(restore_started)
        self.assertFalse(os.path.lexists(target))
        self.assertFalse(os.path.lexists(active))
        cleanup_metadata = journal[0]
        assert cleanup_metadata.parent_identity is not None
        anchor = MODULE._read_pending_regular_publication_private_authority(
            self.home,
            batch,
            record,
            "before",
            journal[3],
            journal[1],
            cleanup_metadata.parent_identity,
        )
        self.assertIsNotNone(anchor)
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

        install(self.release, self.home, SHA_A)

        self.assertFalse(MODULE._pending_link_pointer_path(self.home).exists())
        self.assertFalse(batch.batch_root.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_active_publication_foreign_replacement_is_retained(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        _record, active = self._assert_active_publication_journal(batch)
        active.unlink()
        active.write_text("foreign = true\n", encoding="utf-8")
        active.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending regular publication active entry changed",
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(active.read_text(encoding="utf-8"), "foreign = true\n")
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self.assertTrue(batch.batch_root.is_dir())

    def test_v6_v7_uncommitted_create_and_replace_recover_without_v8_receipts(
        self,
    ) -> None:
        for version in (6, 7):
            for action in ("create", "replace"):
                with self.subTest(version=version, action=action):
                    self.home = self.root / f"home-v{version}-{action}"
                    release = self.release
                    sha = SHA_A
                    old_identity: tuple[int, int] | None = None
                    if action == "replace":
                        install(self.release, self.home, SHA_A)
                        target = self.home / ROLE_TARGET
                        old_identity = (target.stat().st_dev, target.stat().st_ino)
                        release = self.root / f"release-v{version}-{action}"
                        write_release(release, role_payload='name = "updated"\n')
                        sha = SHA_B

                    batch = self._interrupt_uncommitted_regular_publication(
                        release,
                        sha,
                    )
                    parsed = self._downgrade_pending_regular_metadata(batch, version)
                    record = next(
                        candidate
                        for candidate in parsed.records
                        if candidate.is_regular()
                    )
                    self.assertEqual(record.action, action)

                    install(self.release, self.home, SHA_A)

                    target = self.home / ROLE_TARGET
                    self.assertEqual(
                        target.read_text(encoding="utf-8"),
                        'name = "reviewer"\n',
                    )
                    self.assertEqual(target.stat().st_nlink, 1)
                    if old_identity is not None:
                        self.assertEqual(
                            (target.stat().st_dev, target.stat().st_ino),
                            old_identity,
                        )
                    self.assertFalse(
                        os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                    )

    def test_legacy_v6_writer_gid_recovers_uncommitted_create_and_replace(
        self,
    ) -> None:
        for action in ("create", "replace"):
            with self.subTest(action=action):
                self.home = self.root / f"home-v6-writer-{action}"
                release = self.release
                sha = SHA_A
                old_identity: tuple[int, int] | None = None
                if action == "replace":
                    install(self.release, self.home, SHA_A)
                    target = self.home / ROLE_TARGET
                    old_identity = (target.stat().st_dev, target.stat().st_ino)
                    release = self.root / "release-v6-writer-replace"
                    write_release(release, role_payload='name = "updated"\n')
                    sha = SHA_B

                batch = self._interrupt_uncommitted_regular_publication(release, sha)
                payload, legacy_gid = legacy_v6_writer_metadata_payload(batch)
                assert legacy_gid is not None
                write_pending_metadata_payload(batch, payload)
                regular_record = next(
                    record
                    for record in batch.records
                    if record.is_regular() and record.action in {"create", "replace"}
                )
                assert regular_record.stage is not None
                stage = batch.batch_root / Path(*regular_record.stage.parts)
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != legacy_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                os.chown(stage, -1, alternate_gid)

                parsed = MODULE._load_pending_link_batch(self.home)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                parsed_record = next(
                    record for record in parsed.records if record.is_regular()
                )
                self.assertEqual(parsed.metadata_version, 6)
                self.assertEqual(parsed_record.regular_gid, legacy_gid)
                self.assertEqual(stage.stat().st_gid, alternate_gid)

                install(self.release, self.home, SHA_A)

                target = self.home / ROLE_TARGET
                self.assertEqual(
                    target.read_text(encoding="utf-8"),
                    'name = "reviewer"\n',
                )
                self.assertEqual(target.stat().st_nlink, 1)
                if old_identity is not None:
                    self.assertEqual(
                        (target.stat().st_dev, target.stat().st_ino),
                        old_identity,
                    )
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                )

    def test_legacy_v6_writer_remove_recovers_uncommitted_rollback(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        original_identity = (target.stat().st_dev, target.stat().st_ino)
        original_gid = target.stat().st_gid
        removal_release = self.root / "release-v6-writer-remove-uncommitted"
        write_release(removal_release)

        batch = self._interrupt_uncommitted_regular_publication(
            removal_release,
            SHA_B,
        )
        payload, producing_gid = legacy_v6_writer_metadata_payload(batch)
        self.assertIsNone(producing_gid)
        write_pending_metadata_payload(batch, payload)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        self.assertEqual(parsed.metadata_version, 6)
        self.assertEqual(record.action, "remove")
        self.assertIsNone(record.regular_gid)
        self.assertEqual(record.planned_snapshot.regular_gid, original_gid)
        self.assertFalse(os.path.lexists(target))

        install(self.release, self.home, SHA_A)

        self.assertTrue(target.is_file())
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            original_identity,
        )
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_legacy_v6_foreign_regular_gid_recovers_uncommitted_relinquishment(
        self,
    ) -> None:
        initial_release = self.root / "release-v6-foreign-initial"
        next_release = self.root / "release-v6-foreign-next"
        write_release(
            initial_release,
            role_payload='name = "managed"\n',
            target="AGENTS.md",
        )
        write_release(
            next_release,
            role_payload='name = "next"\n',
            target="AGENTS.md",
        )
        install(initial_release, self.home, SHA_A)
        target = self.home / "AGENTS.md"
        target.unlink()
        target.write_text('name = "foreign"\n', encoding="utf-8")
        target.chmod(0o600)
        historical_snapshot = MODULE._capture_reconcile_target_snapshot(
            self.home,
            target,
            capture_regular_content=True,
        )
        historical_gid = historical_snapshot.regular_gid
        self.assertIsNotNone(historical_snapshot.regular_sha256)
        self.assertIsNotNone(historical_gid)
        assert historical_gid is not None

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_rollback_reconcile_transaction",
                side_effect=MODULE.SyncError("injected hard rollback crash"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(next_release, self.home, SHA_B)

        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["version"] = 6
        downgrade_pending_state_evidence_metadata(payload, 6)
        payload.pop("terminal_regular_before")
        payload.pop("terminal_regular_after")
        records = payload["records"]
        assert isinstance(records, list)
        foreign_record = None
        for raw_record in records:
            assert isinstance(raw_record, dict)
            raw_record.pop("before_materialization")
            raw_record.pop("removed_link")
            raw_record.pop("publication_cleanup")
            if raw_record["action"] == MODULE.PENDING_RELINQUISH_FOREIGN_ACTION:
                foreign_record = raw_record
        self.assertIsNotNone(foreign_record)
        assert isinstance(foreign_record, dict)
        planned_before = foreign_record["planned_before"]
        assert isinstance(planned_before, dict)
        planned_before.update(
            {
                "regular_sha256": historical_snapshot.regular_sha256,
                "regular_size": historical_snapshot.regular_size,
                "regular_mode": historical_snapshot.regular_mode,
                "regular_uid": historical_snapshot.regular_uid,
                "regular_gid": historical_gid,
                "regular_link_count": historical_snapshot.regular_link_count,
            }
        )
        write_pending_metadata_payload(batch, payload)

        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != historical_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        os.chown(target, -1, alternate_gid)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        parsed_record = next(
            record
            for record in parsed.records
            if record.action == MODULE.PENDING_RELINQUISH_FOREIGN_ACTION
        )
        self.assertEqual(parsed.metadata_version, 6)
        self.assertEqual(parsed_record.materialization, "symlink")
        self.assertEqual(parsed_record.planned_snapshot.regular_gid, historical_gid)
        self.assertEqual(target.stat().st_gid, alternate_gid)

        install(next_release, self.home, SHA_B)

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "foreign"\n')
        self.assertEqual(target.stat().st_gid, alternate_gid)
        self.assertNotIn(
            PurePosixPath("AGENTS.md"),
            MODULE._load_managed_state(self.home).links,
        )
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_v6_v7_produced_active_alias_residue_recovers_exact_inode(self) -> None:
        for version in (6, 7):
            with self.subTest(version=version):
                self.home = self.root / f"home-v{version}-active-residue"
                batch = self._interrupt_uncommitted_regular_publication(
                    self.release,
                    SHA_A,
                )
                parsed = self._downgrade_pending_regular_metadata(batch, version)
                record = next(
                    candidate for candidate in parsed.records if candidate.is_regular()
                )
                target = self.home / Path(*record.target.parts)
                active = self._isolate_legacy_regular_publication(
                    parsed,
                    record,
                    target,
                )

                install(self.release, self.home, SHA_A)

                self.assertFalse(os.path.lexists(active))
                self.assertEqual(
                    target.read_text(encoding="utf-8"),
                    'name = "reviewer"\n',
                )
                self.assertEqual(target.stat().st_nlink, 1)

    def test_v7_legacy_active_entries_share_one_parent_scan_and_alias_index(
        self,
    ) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            self.release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security"\n',
        )
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        records = tuple(record for record in parsed.records if record.is_regular())
        self.assertEqual(
            {record.target for record in records}, {ROLE_TARGET, secondary_target}
        )
        with mock.patch.object(
            MODULE,
            "_build_pending_regular_alias_authority_index",
            wraps=MODULE._build_pending_regular_alias_authority_index,
        ) as build_alias_index:
            for record in records:
                MODULE._pending_record_evidence_snapshot(self.home, parsed, record)
        self.assertEqual(build_alias_index.call_count, 1)
        active_paths = [
            self._isolate_legacy_regular_publication(
                parsed,
                record,
                self.home / Path(*record.target.parts),
            )
            for record in records
        ]
        parent_identity = (self.home / Path(*ROLE_TARGET.parts)).parent.stat()
        expected_identity = (parent_identity.st_dev, parent_identity.st_ino)
        real_scandir = MODULE.os.scandir
        agent_parent_scans = 0

        def count_agent_parent_scans(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        ) -> object:
            nonlocal agent_parent_scans
            if (
                isinstance(path, int)
                and MODULE._directory_identity(path) == expected_identity
            ):
                agent_parent_scans += 1
            return real_scandir(path)

        with (
            mock.patch.object(
                MODULE.os,
                "scandir",
                side_effect=count_agent_parent_scans,
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(agent_parent_scans, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        for active in active_paths:
            self.assertFalse(os.path.lexists(active))
        for target in (ROLE_TARGET, secondary_target):
            installed = self.home / Path(*target.parts)
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.stat().st_nlink, 1)

    def test_v7_legacy_active_entry_duplicate_candidates_fail_closed(self) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        assert record.planned_snapshot.parent_identity is not None
        assert record.evidence_identity is not None
        duplicate = active.with_name(
            MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                record.planned_snapshot.parent_identity,
                (
                    record.evidence_identity[0],
                    record.evidence_identity[1],
                    stat.S_IFREG,
                ),
            )
        )
        os.link(active, duplicate, follow_symlinks=False)
        index = MODULE._build_legacy_pending_regular_publication_active_entry_index(
            self.home,
            parsed,
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending legacy regular publication cleanup is ambiguous",
        ):
            MODULE._recover_legacy_pending_regular_publication_active_entry(
                self.home,
                parsed,
                record,
                "produced",
                active_entry_index=index,
            )

        self.assertTrue(active.is_file())
        self.assertTrue(duplicate.is_file())
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_v7_legacy_active_entry_batch_scan_budget_fails_before_unlink(self) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            self.release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security"\n',
        )
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        active_paths = [
            self._isolate_legacy_regular_publication(
                parsed,
                record,
                self.home / Path(*record.target.parts),
            )
            for record in parsed.records
            if record.is_regular()
        ]

        with (
            mock.patch.object(MODULE, "MAX_PENDING_CLEANUP_CONTROL_ENTRIES", 1),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending legacy regular publication active-entry scan exceeds the "
                "batch limit",
            ),
        ):
            MODULE._build_legacy_pending_regular_publication_active_entry_index(
                self.home,
                parsed,
            )

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        for active in active_paths:
            self.assertTrue(active.is_file())

    def test_v7_produced_active_alias_foreign_replacement_fails_closed(self) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        active.unlink()
        active.write_text("foreign = true\n", encoding="utf-8")
        active.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending legacy regular publication .* changed",
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(active.read_text(encoding="utf-8"), "foreign = true\n")
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_v7_active_alias_replacement_during_revalidation_is_retained(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        active_entry_index = (
            MODULE._build_legacy_pending_regular_publication_active_entry_index(
                self.home,
                parsed,
            )
        )
        real_snapshot = MODULE._regular_file_snapshot_at
        replaced = False

        def replace_after_first_active_snapshot(
            directory_fd: int,
            name: str,
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal replaced
            snapshot = real_snapshot(
                directory_fd,
                name,
                path,
                *args,
                **kwargs,
            )
            if path == active and not replaced:
                active.unlink()
                active.write_text("foreign = true\n", encoding="utf-8")
                active.chmod(0o600)
                replaced = True
            return snapshot

        with (
            mock.patch.object(
                MODULE,
                "_regular_file_snapshot_at",
                side_effect=replace_after_first_active_snapshot,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending legacy regular publication active entry changed",
            ),
        ):
            MODULE._recover_legacy_pending_regular_publication_active_entry(
                self.home,
                parsed,
                record,
                "produced",
                active_entry_index=active_entry_index,
            )

        self.assertTrue(replaced)
        self.assertEqual(active.read_text(encoding="utf-8"), "foreign = true\n")
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_v7_active_alias_parent_policy_change_after_final_snapshot_fails_closed(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        active_identity = (active.stat().st_dev, active.stat().st_ino)
        active_entry_index = (
            MODULE._build_legacy_pending_regular_publication_active_entry_index(
                self.home,
                parsed,
            )
        )
        real_snapshot = MODULE._regular_file_snapshot_at
        active_snapshots = 0

        def make_parent_writable_after_final_snapshot(
            directory_fd: int,
            name: str,
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal active_snapshots
            snapshot = real_snapshot(directory_fd, name, path, *args, **kwargs)
            if path == active:
                active_snapshots += 1
                if active_snapshots == 3:
                    active.parent.chmod(0o775)
            return snapshot

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_regular_file_snapshot_at",
                    side_effect=make_parent_writable_after_final_snapshot,
                ),
                mock.patch.object(
                    MODULE.os, "unlink", wraps=MODULE.os.unlink
                ) as unlink,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "managed regular-file parent access policy mismatch",
                ),
            ):
                MODULE._recover_legacy_pending_regular_publication_active_entry(
                    self.home,
                    parsed,
                    record,
                    "produced",
                    active_entry_index=active_entry_index,
                )
            unlink.assert_not_called()
            self.assertEqual(active_snapshots, 3)
            self.assertEqual(
                (active.stat().st_dev, active.stat().st_ino), active_identity
            )
            self.assertFalse(os.path.lexists(target))
        finally:
            active.parent.chmod(0o755)

    def test_v7_private_active_alias_recovers_public_to_private_rename_crash(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        index = MODULE._build_legacy_pending_regular_publication_active_entry_index(
            self.home,
            parsed,
        )
        real_rename = MODULE._rename_noreplace_at

        def crash_after_private_rename(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(source_fd, source_name, destination_fd, destination_name)
            if source_name == active.name and source_fd != destination_fd:
                raise MODULE.SyncError("injected private rename crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_private_rename,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "private rename crash"),
        ):
            MODULE._recover_legacy_pending_regular_publication_active_entry(
                self.home,
                parsed,
                record,
                "produced",
                active_entry_index=index,
            )

        cleanup = parsed.batch_root / "pending" / "cleanup"
        cleanup_metadata = cleanup.stat()
        cleanup_identity = (cleanup_metadata.st_dev, cleanup_metadata.st_ino)
        assert record.evidence_identity is not None
        planned = (*record.evidence_identity, stat.S_IFREG)
        private_name = MODULE._pending_regular_publication_private_alias_name(
            cleanup_identity,
            planned,
            record.index,
            "produced",
        )
        self.assertTrue(
            MODULE._pending_batch_cleanup_name_is_authorized(
                ("pending", "cleanup"),
                private_name,
                cleanup_identity,
            )
        )
        self.assertFalse(os.path.lexists(active))
        self.assertTrue((cleanup / private_name).is_file())

        fresh_index = (
            MODULE._build_legacy_pending_regular_publication_active_entry_index(
                self.home,
                parsed,
            )
        )
        MODULE._recover_legacy_pending_regular_publication_active_entry(
            self.home,
            parsed,
            record,
            "produced",
            active_entry_index=fresh_index,
        )
        self.assertFalse(os.path.lexists(cleanup / private_name))

    def test_v6_v7_produced_private_alias_accepts_exact_preimage(self) -> None:
        for version in (6, 7):
            with self.subTest(version=version):
                self.home = self.root / f"home-v{version}-legacy-private-preimage"
                install(self.release, self.home, SHA_A)
                target = self.home / ROLE_TARGET
                old_identity = (target.stat().st_dev, target.stat().st_ino)
                next_release = self.root / f"release-v{version}-legacy-private"
                write_release(next_release, role_payload='name = "updated"\n')
                batch = self._interrupt_uncommitted_regular_publication(
                    next_release,
                    SHA_B,
                )
                parsed = self._downgrade_pending_regular_metadata(batch, version)
                record = next(
                    candidate for candidate in parsed.records if candidate.is_regular()
                )
                active = self._isolate_legacy_regular_publication(
                    parsed,
                    record,
                    target,
                )
                index = (
                    MODULE._build_legacy_pending_regular_publication_active_entry_index(
                        self.home,
                        parsed,
                    )
                )
                real_rename = MODULE._rename_noreplace_at

                def crash_after_private_rename(
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
                    if source_name == active.name and source_fd != destination_fd:
                        raise MODULE.SyncError("injected legacy private rename crash")

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_noreplace_at",
                        side_effect=crash_after_private_rename,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "legacy private rename crash",
                    ),
                ):
                    MODULE._recover_legacy_pending_regular_publication_active_entry(
                        self.home,
                        parsed,
                        record,
                        "produced",
                        active_entry_index=index,
                    )

                cleanup = parsed.batch_root / "pending" / "cleanup"
                cleanup_metadata = cleanup.stat()
                assert record.evidence_identity is not None
                private_name = MODULE._pending_regular_publication_private_alias_name(
                    (cleanup_metadata.st_dev, cleanup_metadata.st_ino),
                    (*record.evidence_identity, stat.S_IFREG),
                    record.index,
                    "produced",
                )
                private = cleanup / private_name
                self.assertTrue(private.is_file())
                self.assertFalse(os.path.lexists(active))
                assert record.before_evidence is not None
                before = parsed.batch_root / Path(*record.before_evidence.parts)
                os.link(before, target, follow_symlinks=False)
                self.assertEqual(
                    (target.stat().st_dev, target.stat().st_ino),
                    old_identity,
                )

                fresh_index = (
                    MODULE._build_legacy_pending_regular_publication_active_entry_index(
                        self.home,
                        parsed,
                    )
                )
                MODULE._recover_legacy_pending_regular_publication_active_entry(
                    self.home,
                    parsed,
                    record,
                    "produced",
                    active_entry_index=fresh_index,
                )

                self.assertFalse(os.path.lexists(private))
                self.assertEqual(
                    (target.stat().st_dev, target.stat().st_ino),
                    old_identity,
                )
                install(self.release, self.home, SHA_A)
                self.assertFalse(parsed.batch_root.exists())
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                )
                self.assertEqual(
                    (target.stat().st_dev, target.stat().st_ino),
                    old_identity,
                )
                self.assertEqual(target.stat().st_nlink, 1)

    def test_v7_before_private_alias_rejects_canonical_reappearance_after_unlink(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        next_release = self.root / "next-release-v7-before-private"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_uncommitted_regular_publication(next_release, SHA_B)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        assert record.before_evidence is not None
        before = parsed.batch_root / Path(*record.before_evidence.parts)
        parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
        try:
            parent_identity = MODULE._directory_identity(parent_fd)
            before_metadata = before.stat()
            planned = MODULE._pending_cleanup_entry_plan(before_metadata)
            active_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                parent_identity,
                planned,
            )
            active = target.with_name(active_name)
            os.link(before, active, follow_symlinks=False)
            os.fsync(parent_fd)
        finally:
            MODULE._close_fd_quietly(parent_fd)

        index = MODULE._build_legacy_pending_regular_publication_active_entry_index(
            self.home,
            parsed,
        )
        real_rename = MODULE._rename_noreplace_at

        def crash_after_private_rename(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(source_fd, source_name, destination_fd, destination_name)
            if source_name == active.name and source_fd != destination_fd:
                raise MODULE.SyncError("injected before private rename crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_private_rename,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "before private rename crash"),
        ):
            MODULE._recover_legacy_pending_regular_publication_active_entry(
                self.home,
                parsed,
                record,
                "before",
                active_entry_index=index,
            )

        cleanup = parsed.batch_root / "pending" / "cleanup"
        cleanup_stat = cleanup.stat()
        assert record.before_evidence_identity is not None
        private_name = MODULE._pending_regular_publication_private_alias_name(
            (cleanup_stat.st_dev, cleanup_stat.st_ino),
            (*record.before_evidence_identity, stat.S_IFREG),
            record.index,
            "before",
        )
        self.assertTrue((cleanup / private_name).is_file())
        self.assertFalse(os.path.lexists(active))
        target.unlink()

        fresh_index = (
            MODULE._build_legacy_pending_regular_publication_active_entry_index(
                self.home,
                parsed,
            )
        )
        real_unlink = MODULE.os.unlink
        restored = False

        def restore_canonical_after_private_unlink(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal restored
            real_unlink(name, *args, **kwargs)  # type: ignore[arg-type]
            if (
                MODULE._pending_regular_publication_private_deletion_alias_base(name)
                == private_name
                and not restored
            ):
                os.link(before, target, follow_symlinks=False)
                restored = True

        with (
            mock.patch.object(
                MODULE.os,
                "unlink",
                side_effect=restore_canonical_after_private_unlink,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "public name reappeared while private cleanup completed",
            ),
        ):
            MODULE._recover_legacy_pending_regular_publication_active_entry(
                self.home,
                parsed,
                record,
                "before",
                active_entry_index=fresh_index,
            )

        self.assertTrue(restored)
        self.assertFalse(os.path.lexists(cleanup / private_name))
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self._assert_recovered_install()

    def test_v7_final_public_snapshot_replacement_is_preserved_privately(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        index = MODULE._build_legacy_pending_regular_publication_active_entry_index(
            self.home,
            parsed,
        )
        real_snapshot = MODULE._regular_file_snapshot_at
        active_snapshots = 0

        def replace_after_final_public_snapshot(
            directory_fd: int,
            name: str,
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal active_snapshots
            snapshot = real_snapshot(directory_fd, name, path, *args, **kwargs)
            if path == active:
                active_snapshots += 1
                if active_snapshots == 3:
                    active.unlink()
                    active.write_text("foreign = true\n", encoding="utf-8")
                    active.chmod(0o600)
            return snapshot

        with (
            mock.patch.object(
                MODULE,
                "_regular_file_snapshot_at",
                side_effect=replace_after_final_public_snapshot,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed during private isolation",
            ),
        ):
            MODULE._recover_legacy_pending_regular_publication_active_entry(
                self.home,
                parsed,
                record,
                "produced",
                active_entry_index=index,
            )

        self.assertEqual(active_snapshots, 3)
        self.assertFalse(os.path.lexists(active))
        retained = tuple(
            child
            for child in (parsed.batch_root / "pending" / "cleanup").iterdir()
            if child.name.startswith(MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX)
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(encoding="utf-8"), "foreign = true\n")

    def test_v9_receiptless_remove_active_alias_rejects_writable_parent(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        removal_release = self.root / "release-v9-remove-writable-parent"
        write_release(removal_release)
        batch = self._interrupt_uncommitted_regular_publication(
            removal_release,
            SHA_B,
        )
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["version"] = 9
        records = payload["records"]
        assert isinstance(records, list)
        for raw_record in records:
            assert isinstance(raw_record, dict)
            raw_record.pop("before_materialization")
            raw_record.pop("removed_link")
        removal_record = next(
            raw_record
            for raw_record in records
            if raw_record["target"] == ROLE_TARGET.as_posix()
        )
        removal_record["publication_cleanup"] = None
        write_pending_metadata_payload(batch, payload)
        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        record = next(
            candidate for candidate in parsed.records if candidate.target == ROLE_TARGET
        )
        before = MODULE._pending_record_before_evidence_snapshot(
            self.home,
            parsed,
            record,
        )
        self.assertIsInstance(before, MODULE.RegularFileSnapshot)
        assert isinstance(before, MODULE.RegularFileSnapshot)
        MODULE._restore_pending_record_before(self.home, parsed, record, before)
        target = self.home / ROLE_TARGET
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        target.parent.chmod(0o775)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed regular-file parent access policy mismatch",
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(active.is_file())
        self.assertTrue(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_v7_before_active_alias_residue_recovers_exact_preimage(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release-v7-before"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_uncommitted_regular_publication(next_release, SHA_B)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        assert record.before_evidence is not None
        before = parsed.batch_root / Path(*record.before_evidence.parts)
        parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
        try:
            parent_identity = MODULE._directory_identity(parent_fd)
            before_metadata = before.stat()
            planned = MODULE._pending_cleanup_entry_plan(before_metadata)
            active_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                parent_identity,
                planned,
            )
            active = target.with_name(active_name)
            os.link(before, active, follow_symlinks=False)
            os.fsync(parent_fd)
        finally:
            MODULE._close_fd_quietly(parent_fd)
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "updated"\n')
        self.assertEqual((active.stat().st_dev, active.stat().st_ino), old_identity)

        install(self.release, self.home, SHA_A)

        self.assertFalse(os.path.lexists(active))
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_publication_receipt_recovers_truncated_atomic_temp(self) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        expected, exists = MODULE._pending_target_snapshot(self.home, target)
        self.assertTrue(exists)
        assert isinstance(expected, MODULE.RegularFileSnapshot)
        journal = MODULE._pending_regular_publication_cleanup_path(
            batch,
            record,
            "produced",
        )
        temp = journal.with_name(
            journal.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        )
        cleanup_parent_identity = (batch.batch_root / "pending" / "cleanup").stat()
        parent_identity = (
            cleanup_parent_identity.st_dev,
            cleanup_parent_identity.st_ino,
        )
        retained_temp_name = (
            MODULE.PENDING_CLEANUP_RETAINED_PREFIX + temp.name + "-1-" + "a" * 16
        )
        self.assertTrue(
            MODULE._pending_batch_cleanup_name_is_authorized(
                ("pending", "cleanup"),
                temp.name,
                parent_identity,
            )
        )
        self.assertTrue(
            MODULE._pending_batch_cleanup_name_is_authorized(
                ("pending", "cleanup"),
                retained_temp_name,
                parent_identity,
            )
        )
        self.assertFalse(
            MODULE._pending_batch_cleanup_name_is_authorized(
                ("pending", "cleanup"),
                "foreign.json.publish-tmp",
                parent_identity,
            )
        )
        temp.write_bytes(b"{")
        temp.chmod(0o600)

        MODULE._delete_pending_regular_publication_beneath(
            self.home,
            batch,
            record,
            target,
            expected,
            phase="produced",
        )

        self.assertFalse(os.path.lexists(temp))
        self.assertFalse(os.path.lexists(target))
        self.assertIsNotNone(
            MODULE._read_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )
        )

    def test_publication_receipt_is_complete_after_atomic_rename_boundary_crash(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        expected, exists = MODULE._pending_target_snapshot(self.home, target)
        self.assertTrue(exists)
        assert isinstance(expected, MODULE.RegularFileSnapshot)
        journal = MODULE._pending_regular_publication_cleanup_path(
            batch,
            record,
            "produced",
        )
        temp_name = journal.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        real_rename = MODULE._rename_noreplace_at
        crashed = False

        def crash_after_atomic_publication(
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
            if source_name == temp_name and destination_name == journal.name:
                crashed = True
                raise MODULE.SyncError("injected atomic rename boundary crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_atomic_publication,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "atomic rename boundary crash"),
        ):
            MODULE._delete_pending_regular_publication_beneath(
                self.home,
                batch,
                record,
                target,
                expected,
                phase="produced",
            )

        self.assertTrue(crashed)
        self.assertTrue(target.is_file())
        self.assertTrue(journal.is_file())
        self.assertFalse(os.path.lexists(journal.with_name(temp_name)))
        self.assertIsNotNone(
            MODULE._read_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )
        )

        MODULE._recover_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        self.assertFalse(os.path.lexists(target))

    def test_prepared_receipt_recovers_crash_after_active_rename(self) -> None:
        install(self.release, self.home, SHA_A)
        next_release = self.root / "next-release-prepared-receipt"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_uncommitted_regular_publication(next_release, SHA_B)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)

        for phase in ("produced", "before"):
            with self.subTest(phase=phase):
                if phase == "before":
                    before = MODULE._pending_record_before_evidence_snapshot(
                        self.home,
                        batch,
                        record,
                    )
                    MODULE._restore_pending_record_before(
                        self.home,
                        batch,
                        record,
                        before,
                    )

                expected, exists = MODULE._pending_target_snapshot(self.home, target)
                self.assertTrue(exists)
                assert isinstance(expected, MODULE.RegularFileSnapshot)
                real_rename = MODULE._rename_noreplace_at
                crashed = False

                def crash_after_active_rename(
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
                    if source_name == target.name and destination_name.startswith(
                        MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                    ):
                        crashed = True
                        raise MODULE.SyncError(
                            f"injected {phase} crash after active rename"
                        )

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_noreplace_at",
                        side_effect=crash_after_active_rename,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        f"injected {phase} crash after active rename",
                    ),
                ):
                    MODULE._delete_pending_regular_publication_beneath(
                        self.home,
                        batch,
                        record,
                        target,
                        expected,
                        phase=phase,
                    )

                self.assertTrue(crashed)
                self.assertFalse(os.path.lexists(target))
                journal = MODULE._read_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    phase,
                )
                self.assertIsNotNone(journal)
                assert journal is not None
                _snapshot, active_name, _journal_phase, _journal_expected = journal
                active = target.with_name(active_name)
                self.assertTrue(active.is_file())

                def crash_after_private_rename(
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
                    if source_name == active_name and source_fd != destination_fd:
                        raise MODULE.SyncError(
                            f"injected {phase} crash after private rename"
                        )

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_noreplace_at",
                        side_effect=crash_after_private_rename,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        f"injected {phase} crash after private rename",
                    ),
                ):
                    MODULE._recover_pending_regular_publication_cleanup(
                        self.home,
                        batch,
                        record,
                        phase,
                    )
                self.assertFalse(os.path.lexists(active))
                cleanup = batch.batch_root / "pending" / "cleanup"
                cleanup_metadata = cleanup.stat()
                cleanup_identity = (cleanup_metadata.st_dev, cleanup_metadata.st_ino)
                planned = (
                    _journal_expected.file_identity[0],
                    _journal_expected.file_identity[1],
                    stat.S_IFREG,
                )
                private_name = MODULE._pending_regular_publication_private_alias_name(
                    cleanup_identity,
                    planned,
                    record.index,
                    phase,
                )
                self.assertTrue((cleanup / private_name).is_file())

                MODULE._recover_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    phase,
                )
                self.assertFalse(os.path.lexists(cleanup / private_name))

    def test_durable_active_alias_parent_policy_change_after_final_snapshot_fails_closed(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        expected, exists = MODULE._pending_target_snapshot(self.home, target)
        self.assertTrue(exists)
        assert isinstance(expected, MODULE.RegularFileSnapshot)
        real_rename = MODULE._rename_noreplace_at

        def stop_after_public_active_rename(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(source_fd, source_name, destination_fd, destination_name)
            if source_name == target.name and destination_name.startswith(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
            ):
                raise MODULE.SyncError("injected public active rename stop")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=stop_after_public_active_rename,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "public active rename stop"),
        ):
            MODULE._delete_pending_regular_publication_beneath(
                self.home,
                batch,
                record,
                target,
                expected,
                phase="produced",
            )
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        assert journal is not None
        _snapshot, active_name, _journal_phase, _journal_expected = journal
        active = target.with_name(active_name)
        active_identity = (active.stat().st_dev, active.stat().st_ino)
        real_snapshot = MODULE._regular_file_snapshot_at
        active_snapshots = 0

        def make_parent_writable_after_final_snapshot(
            directory_fd: int,
            name: str,
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal active_snapshots
            snapshot = real_snapshot(directory_fd, name, path, *args, **kwargs)
            if path == active:
                active_snapshots += 1
                if active_snapshots == 2:
                    active.parent.chmod(0o775)
            return snapshot

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_regular_file_snapshot_at",
                    side_effect=make_parent_writable_after_final_snapshot,
                ),
                mock.patch.object(
                    MODULE.os, "unlink", wraps=MODULE.os.unlink
                ) as unlink,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "managed regular-file parent access policy mismatch",
                ),
            ):
                MODULE._recover_pending_regular_publication_cleanup(
                    self.home,
                    batch,
                    record,
                    "produced",
                )
            unlink.assert_not_called()
            self.assertEqual(active_snapshots, 2)
            self.assertEqual(
                (active.stat().st_dev, active.stat().st_ino), active_identity
            )
            self.assertFalse(os.path.lexists(target))
        finally:
            active.parent.chmod(0o755)

    def test_pending_cleanup_isolates_before_canonical_snapshot(self) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        expected, exists = MODULE._pending_target_snapshot(self.home, target)
        self.assertTrue(exists)
        assert isinstance(expected, MODULE.RegularFileSnapshot)
        real_snapshot = MODULE._regular_file_snapshot_at
        canonical_reads = 0

        def reject_canonical_snapshot(
            directory_fd: int,
            name: str,
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal canonical_reads
            if path == target:
                canonical_reads += 1
                raise OSError("canonical publication must already be isolated")
            return real_snapshot(
                directory_fd,
                name,
                path,
                *args,
                **kwargs,
            )

        with mock.patch.object(
            MODULE,
            "_regular_file_snapshot_at",
            side_effect=reject_canonical_snapshot,
        ):
            MODULE._delete_pending_regular_publication_beneath(
                self.home,
                batch,
                record,
                target,
                expected,
                phase="produced",
            )

        self.assertEqual(canonical_reads, 0)
        self.assertFalse(os.path.lexists(target))
        self.assertIsNotNone(
            MODULE._read_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )
        )

    def test_pending_cleanup_retains_canonical_mismatch_off_active_path(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        record = next(
            candidate for candidate in batch.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        expected, exists = MODULE._pending_target_snapshot(self.home, target)
        self.assertTrue(exists)
        assert isinstance(expected, MODULE.RegularFileSnapshot)
        target.unlink()
        target.write_text('name = "foreign"\n', encoding="utf-8")
        target.chmod(0o600)

        with self.assertRaisesRegex(MODULE.SyncError, "preserved as"):
            MODULE._delete_pending_regular_publication_beneath(
                self.home,
                batch,
                record,
                target,
                expected,
                phase="produced",
            )

        self.assertFalse(os.path.lexists(target))
        retained = tuple(
            child
            for child in target.parent.iterdir()
            if child.name.startswith(MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX)
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            retained[0].read_text(encoding="utf-8"),
            'name = "foreign"\n',
        )

    def test_precommit_crash_recovery_retries_regular_publication(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_precommit_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "before":
                raise MODULE.SyncError("injected precommit pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_precommit_pointer,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        ticket = MODULE._read_pending_cleanup_ticket(
            self.home,
            MODULE._pending_cleanup_ticket_path(
                self.home,
                batch.batch_root.name,
            ),
        )
        # A failed first install has no before-state regular target, so v7 does
        # not mint terminal cleanup authority for the rolled-back phase.
        self.assertIsNone(ticket)
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)
        self.assertTrue(batch.batch_root.is_dir())
        self._assert_recovered_install()

    def test_post_clear_cleanup_failure_is_retried_from_durable_ticket(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def fail_after_pointer_clear(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            real_clear(home, batch, phase=phase)
            if phase == "after":
                raise MODULE.SyncError("injected post-clear cleanup failure")

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=fail_after_pointer_clear,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertTrue(any(ticket_root.glob("*.json")))
        self._assert_recovered_install()

    def test_regular_update_rollback_restores_old_file_and_final_link_count(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        real_clear = MODULE._clear_pending_link_pointer

        def retain_precommit_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "before":
                raise MODULE.SyncError("injected precommit pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_precommit_pointer,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(next_release, self.home, SHA_B)

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertGreater(target.stat().st_nlink, 1)

        install(self.release, self.home, SHA_A)

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_committed_crash_recovery_finalizes_regular_publication(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected committed pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_committed_pointer,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self._assert_recovered_install()

    def test_committed_recovery_tolerates_non_access_bearing_gid_churn(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected committed pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_committed_pointer,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        os.chown(target, -1, alternate_gid)

        self._assert_recovered_install()
        self.assertEqual(target.stat().st_gid, alternate_gid)

    def test_live_transaction_tolerates_mode_0600_gid_churn(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "reviewer"\n')
        append_regular_link(
            next_release,
            target="agents/security-reviewer.toml",
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )
        real_capture = MODULE._capture_managed_state_link_snapshots
        captures = 0
        churned = False

        def churn_gid_after_live_baseline(
            home: Path,
            state: MODULE.ManagedState,
        ) -> dict[
            PurePosixPath,
            MODULE.SymlinkSnapshot | MODULE.RegularFileSnapshot,
        ]:
            nonlocal captures, churned
            snapshots = real_capture(home, state)
            captures += 1
            # install_release_tree performs an unlocked preflight first. Drift
            # only after the locked transaction's baseline has been captured.
            if captures == 2:
                os.chown(target, -1, alternate_gid)
                churned = True
            return snapshots

        with mock.patch.object(
            MODULE,
            "_capture_managed_state_link_snapshots",
            side_effect=churn_gid_after_live_baseline,
        ):
            install(next_release, self.home, SHA_B)

        self.assertTrue(churned)
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(target.stat().st_gid, alternate_gid)

    def test_replace_tolerates_mode_0600_gid_churn_during_destructive_staging(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        real_move = MODULE._atomic_move_beneath_home
        churned = False

        def churn_gid_before_move(*args: object, **kwargs: object) -> None:
            nonlocal churned
            if args[1] == target:
                os.chown(target, -1, alternate_gid)
                churned = True
            real_move(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=churn_gid_before_move,
        ):
            install(next_release, self.home, SHA_B)

        self.assertTrue(churned)
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "updated"\n')

    def test_removal_tolerates_mode_0600_gid_churn_during_destructive_staging(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        next_release = self.root / "next-release"
        write_release(next_release)
        real_move = MODULE._atomic_move_beneath_home
        churned = False

        def churn_gid_before_move(*args: object, **kwargs: object) -> None:
            nonlocal churned
            if args[1] == target:
                os.chown(target, -1, alternate_gid)
                churned = True
            real_move(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=churn_gid_before_move,
        ):
            install(next_release, self.home, SHA_B)

        self.assertTrue(churned)
        self.assertFalse(os.path.lexists(target))

    def test_replace_rollback_tolerates_mode_0600_backup_gid_churn(self) -> None:
        target = self.home / ROLE_TARGET
        backup = self.home / "personal-sync" / "quarantine" / "rollback" / ROLE_TARGET
        target.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o600)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )
        MODULE._atomic_move_beneath_home(
            self.home,
            target,
            backup,
            planned,
            backup_parent_identity,
        )
        os.chown(backup, -1, alternate_gid)
        action = MODULE.ReconcileAction(
            action="replace",
            target=target,
            link_target="",
            kind="file",
            planned_snapshot=planned,
            materialization="regular",
        )
        MODULE._rollback_reconcile_transaction(
            self.home,
            MODULE.ReconcileTransaction(
                batch_root=None,
                mutations=[MODULE.ReconcileMutation(action=action, backup=backup)],
            ),
        )

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(target.stat().st_gid, alternate_gid)
        self.assertFalse(os.path.lexists(backup))

    def test_destructive_move_rejects_gid_churn_when_group_access_is_granted(
        self,
    ) -> None:
        target = self.home / ROLE_TARGET
        backup = self.home / "personal-sync" / "quarantine" / "reviewer.toml"
        target.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o640)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        os.chown(target, -1, alternate_gid)
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )

        with self.assertRaisesRegex(MODULE.SyncError, "source changed after planning"):
            MODULE._atomic_move_beneath_home(
                self.home,
                target,
                backup,
                planned,
                backup_parent_identity,
            )

        self.assertTrue(target.is_file())
        self.assertFalse(os.path.lexists(backup))

    def test_destructive_backup_rejects_gid_churn_when_group_access_is_granted(
        self,
    ) -> None:
        target = self.home / ROLE_TARGET
        backup = self.home / "personal-sync" / "quarantine" / "reviewer.toml"
        target.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o640)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )
        MODULE._atomic_move_beneath_home(
            self.home,
            target,
            backup,
            planned,
            backup_parent_identity,
        )
        os.chown(backup, -1, alternate_gid)
        action = MODULE.ReconcileAction(
            action="remove",
            target=target,
            link_target="",
            kind="file",
            planned_snapshot=planned,
            materialization="regular",
        )

        with self.assertRaisesRegex(MODULE.SyncError, "target changed after preflight"):
            MODULE._verify_reconcile_backup(self.home, action, backup)

        self.assertFalse(os.path.lexists(target))
        self.assertTrue(backup.is_file())

    def test_terminal_ticket_validates_complete_regular_target_group(self) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        secondary_source = (
            self.release / "personal_codex" / "agents" / "security-reviewer.toml"
        )
        secondary_source.write_text('name = "security-reviewer"\n', encoding="utf-8")
        manifest_path = self.release / MODULE.MANIFEST_RELATIVE_PATH
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["links"].append(
            {
                "source": "personal_codex/agents/security-reviewer.toml",
                "target": secondary_target.as_posix(),
                "kind": "file",
                "owner": MODULE.PUBLIC_OWNER,
            }
        )
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        real_verify = MODULE._verify_final_regular_targets
        captured: list[MODULE.PendingBatchCleanupTicket] = []

        def tamper_second_then_verify(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            captured.append(ticket)
            target = home / Path(*secondary_target.parts)
            target.write_text("tampered = true\n", encoding="utf-8")
            target.chmod(0o600)
            real_verify(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=tamper_second_then_verify,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            tuple(
                expectation.target
                for expectation in captured[0].terminal_regular_targets
            ),
            (ROLE_TARGET, secondary_target),
        )
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertEqual(len(list(ticket_root.glob("*.json"))), 1)
        self.assertEqual(len(list(ticket_root.glob("*.empty-proof"))), 1)

    def test_terminal_validation_rechecks_first_target_after_later_member(self) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            self.release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )
        first_target = self.home / ROLE_TARGET
        real_verify = MODULE._verify_final_regular_targets
        real_read = MODULE._read_regular_file_snapshot_beneath
        validating_terminal_group = False
        drifted = False

        def drift_first_while_reading_later(
            home: Path,
            path: Path,
            *,
            require_managed_access: bool,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal drifted
            snapshot = real_read(
                home,
                path,
                require_managed_access=require_managed_access,
            )
            if (
                validating_terminal_group
                and not drifted
                and path == home / Path(*secondary_target.parts)
            ):
                first_target.write_text("tampered = true\n", encoding="utf-8")
                first_target.chmod(0o600)
                drifted = True
            return snapshot

        def verify_with_mid_pass_drift(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            nonlocal validating_terminal_group
            validating_terminal_group = True
            try:
                real_verify(home, ticket)
            finally:
                validating_terminal_group = False

        with (
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                side_effect=drift_first_while_reading_later,
            ),
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=verify_with_mid_pass_drift,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(drifted)


class PendingMetadataCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_projected_planned_snapshot_covers_every_v6_field(self) -> None:
        target = self.home / ROLE_TARGET
        absent = MODULE.ReconcileTargetSnapshot(
            parent_identity=(1, 2),
            ancestor_identity=(1, 2),
        )
        projected_absent = MODULE._projected_pending_snapshot_payload(
            self.home,
            target,
            absent,
        )
        actual_absent = MODULE._planned_snapshot_payload(absent)
        self.assertEqual(set(projected_absent), set(actual_absent))
        for field in (
            "regular_sha256",
            "regular_size",
            "regular_mode",
            "regular_uid",
            "regular_gid",
            "regular_link_count",
        ):
            self.assertIsNone(projected_absent[field])

        regular = MODULE.ReconcileTargetSnapshot(
            parent_identity=(1, 2),
            link_identity=(3, 4),
            ancestor_identity=(1, 2),
            regular_sha256="a" * 64,
            regular_size=99,
            regular_mode=0o600,
            regular_uid=501,
            regular_gid=20,
            regular_link_count=9,
        )
        projected_regular = MODULE._projected_pending_snapshot_payload(
            self.home,
            target,
            regular,
        )
        actual_regular = MODULE._planned_snapshot_payload(regular)
        self.assertEqual(set(projected_regular), set(actual_regular))
        self.assertGreaterEqual(
            MODULE._projected_json_size(projected_regular, trailing_newline=False),
            MODULE._projected_json_size(actual_regular, trailing_newline=False),
        )

    def test_v8_metadata_projection_includes_empty_terminal_regular_arrays(
        self,
    ) -> None:
        payload = MODULE._projected_pending_metadata_payload(
            state_before_exists=False,
            records=[],
            claims_before=[],
            claims_after=[],
            releases_before=[],
            releases_after=[],
            terminal_regular_before=[],
            terminal_regular_after=[],
        )

        self.assertEqual(payload["version"], MODULE.PENDING_LINK_METADATA_VERSION)
        self.assertEqual(payload["terminal_regular_before"], [])
        self.assertEqual(payload["terminal_regular_after"], [])

    def test_runtime_metadata_capacity_projects_terminal_regular_states(self) -> None:
        profile = MODULE._manifest_transition_capacity_profile(
            MODULE.PUBLIC_OWNER,
            {
                ROLE_TARGET.as_posix(): {
                    "source": "personal_codex/agents/reviewer.toml",
                    "target": ROLE_TARGET.as_posix(),
                    "kind": "file",
                }
            },
            {},
        )
        capacity = MODULE.PendingLinkCapacityPlan(
            ordered_groups=(),
            flattened_actions=(),
            retired_absence_specs=(),
        )
        with mock.patch.object(
            MODULE,
            "_bounded_json_document",
            wraps=MODULE._bounded_json_document,
        ) as encode:
            MODULE._validate_pending_link_metadata_capacity(
                self.home,
                capacity,
                MODULE.ManagedStateFileSnapshot(exists=True),
                profile.state,
                profile.state,
                profile.state,
            )

        projected = encode.call_args.args[0]
        self.assertEqual(
            [item["target"] for item in projected["terminal_regular_before"]],
            [ROLE_TARGET.as_posix()],
        )
        self.assertEqual(
            [item["target"] for item in projected["terminal_regular_after"]],
            [ROLE_TARGET.as_posix()],
        )

    def test_manifest_transition_capacity_counts_terminal_regular_arrays(self) -> None:
        profile = MODULE._manifest_transition_capacity_profile(
            MODULE.PUBLIC_OWNER,
            {
                ROLE_TARGET.as_posix(): {
                    "source": "personal_codex/agents/reviewer.toml",
                    "target": ROLE_TARGET.as_posix(),
                    "kind": "file",
                }
            },
            {},
        )
        without_terminal_regular = MODULE.replace(
            profile,
            terminal_regular_size_sum=0,
            terminal_regular_count=0,
        )

        self.assertEqual(profile.terminal_regular_count, 1)
        self.assertGreater(
            MODULE._manifest_transition_metadata_size(profile, profile),
            MODULE._manifest_transition_metadata_size(
                without_terminal_regular,
                without_terminal_regular,
            ),
        )

    def test_regular_gid_policy_only_ignores_owner_only_gid_drift(self) -> None:
        for mode, expected_match in (
            (0o600, True),
            (0o700, True),
            (0o2700, False),
            (0o604, False),
            (0o640, False),
        ):
            with self.subTest(mode=oct(mode)):
                self.assertEqual(
                    MODULE._gid_matches_regular_file_access_policy(80, 20, mode),
                    expected_match,
                )

    def test_regular_snapshot_gid_comparison_follows_group_access(self) -> None:
        expected = MODULE.RegularFileSnapshot(
            parent_identity=(1, 2),
            file_identity=(3, 4),
            sha256="a" * 64,
            size=10,
            mode=0o640,
            uid=501,
            gid=20,
            link_count=1,
        )
        actual = MODULE.RegularFileSnapshot(
            parent_identity=expected.parent_identity,
            file_identity=expected.file_identity,
            sha256=expected.sha256,
            size=expected.size,
            mode=expected.mode,
            uid=expected.uid,
            gid=80,
            link_count=expected.link_count,
        )

        self.assertFalse(
            MODULE._regular_snapshot_matches(
                actual,
                expected.parent_identity,
                expected,
                expected_link_count=1,
            )
        )
        non_access_bearing_expected = MODULE.replace(expected, mode=0o600)
        non_access_bearing_actual = MODULE.replace(actual, mode=0o600)
        self.assertTrue(
            MODULE._regular_snapshot_matches(
                non_access_bearing_actual,
                non_access_bearing_expected.parent_identity,
                non_access_bearing_expected,
                expected_link_count=1,
            )
        )

    def test_reconcile_target_gid_revalidation_follows_group_access(self) -> None:
        expected = MODULE.ReconcileTargetSnapshot(
            parent_identity=(1, 2),
            link_identity=(3, 4),
            ancestor_identity=(1, 2),
            regular_sha256="a" * 64,
            regular_size=10,
            regular_mode=0o600,
            regular_uid=501,
            regular_gid=20,
            regular_link_count=1,
        )
        actual = MODULE.replace(expected, regular_gid=80)
        target = self.home / ROLE_TARGET

        with mock.patch.object(
            MODULE,
            "_capture_reconcile_target_snapshot",
            return_value=actual,
        ):
            MODULE._require_reconcile_target_snapshot(self.home, target, expected)

        group_expected = MODULE.replace(expected, regular_mode=0o640)
        group_actual = MODULE.replace(actual, regular_mode=0o640)
        with (
            mock.patch.object(
                MODULE,
                "_capture_reconcile_target_snapshot",
                return_value=group_actual,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed target changed after planning",
            ),
        ):
            MODULE._require_reconcile_target_snapshot(
                self.home,
                target,
                group_expected,
            )

    def test_managed_file_evidence_gid_comparison_follows_group_access(self) -> None:
        expected = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"abc",
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=3,
            uid=501,
            gid=20,
        )
        actual = MODULE.replace(expected, gid=80)

        self.assertTrue(
            MODULE._managed_state_snapshot_matches_bound_file_evidence(
                actual,
                expected,
            )
        )
        group_expected = MODULE.replace(expected, mode=0o640)
        group_actual = MODULE.replace(actual, mode=0o640)
        self.assertFalse(
            MODULE._managed_state_snapshot_matches_bound_file_evidence(
                group_actual,
                group_expected,
            )
        )
        self.assertFalse(
            MODULE._managed_state_snapshot_matches_bound_file_evidence(
                MODULE.replace(actual, parent_identity=(9, 9)),
                expected,
            )
        )
        for field, value in (
            ("file_identity", (9, 9)),
            ("payload", b"abd"),
            ("mode", 0o640),
            ("uid", 502),
        ):
            with self.subTest(field=field):
                self.assertFalse(
                    MODULE._managed_state_snapshot_matches_bound_file_evidence(
                        MODULE.replace(actual, **{field: value}),
                        expected,
                    )
                )

    def test_control_snapshot_rechecks_tolerate_owner_only_gid_churn(self) -> None:
        snapshot = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"control\n",
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=8,
            uid=os.geteuid(),
            gid=20,
        )
        churned = MODULE.replace(snapshot, gid=80)
        absent = MODULE.ManagedStateFileSnapshot(exists=False)
        control_path = self.home / "control" / "marker"

        with (
            mock.patch.object(MODULE, "_open_directory_beneath", return_value=10),
            mock.patch.object(MODULE, "_close_fd_quietly"),
            mock.patch.object(
                MODULE,
                "_read_managed_state_file_snapshot",
                side_effect=(absent, churned, churned),
            ),
            mock.patch.object(
                MODULE,
                "_write_exclusive_internal_file",
                return_value=snapshot,
            ),
            mock.patch.object(MODULE, "_discard_incomplete_pending_cleanup_ticket"),
            mock.patch.object(
                MODULE,
                "_pending_cleanup_temp_residue_is_observed",
                return_value=False,
            ),
            mock.patch.object(MODULE, "_rename_noreplace_at"),
            mock.patch.object(MODULE.os, "fsync"),
        ):
            published = MODULE._publish_atomic_exclusive_internal_file(
                self.home,
                control_path,
                snapshot.payload,
            )
        self.assertEqual(published, churned)

        state_before = MODULE.ManagedStateFileSnapshot(
            exists=False,
            parent_identity=(5, 6),
        )
        rollback_batch = SimpleNamespace(
            batch_root=self.home / "20000101T000000Z-1-1",
            state_before=state_before,
        )
        with (
            mock.patch.object(
                MODULE,
                "_pending_rollback_marker_snapshot",
                side_effect=(None, churned),
            ),
            mock.patch.object(
                MODULE,
                "_publish_atomic_exclusive_internal_file",
                return_value=snapshot,
            ),
        ):
            self.assertEqual(
                MODULE._publish_pending_rollback_marker(
                    self.home,
                    rollback_batch,
                ),
                churned,
            )

        batch_root = self.home / "20000101T000000Z-1-2"
        staging_ticket = SimpleNamespace(
            version=3,
            phase="staging",
            batch_root_identity=(7, 8),
            snapshot=SimpleNamespace(payload=b"ticket\n"),
        )
        with (
            mock.patch.object(
                MODULE,
                "_publish_atomic_exclusive_internal_file",
                return_value=snapshot,
            ),
            mock.patch.object(
                MODULE,
                "_pending_staging_marker_snapshot",
                return_value=churned,
            ),
            mock.patch.object(
                MODULE,
                "_pending_staging_cleanup_ticket_payload",
                return_value=b"ticket\n",
            ),
            mock.patch.object(
                MODULE,
                "_publish_pending_batch_cleanup_ticket_for_root",
            ),
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=staging_ticket,
            ),
        ):
            self.assertIs(
                MODULE._mark_pending_batch_staging_cleanup_ready(
                    self.home,
                    batch_root,
                    (7, 8),
                ),
                staging_ticket,
            )

        proof_ticket = SimpleNamespace(batch_root=self.home / "20000101T000000Z-1-3")
        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_empty_proof",
                side_effect=(None, churned),
            ),
            mock.patch.object(
                MODULE,
                "_publish_atomic_exclusive_internal_file",
                return_value=snapshot,
            ),
            mock.patch.object(
                MODULE,
                "_pending_cleanup_empty_proof_payload",
                return_value=snapshot.payload,
            ),
        ):
            self.assertEqual(
                MODULE._publish_pending_cleanup_empty_proof(
                    self.home,
                    proof_ticket,
                    (9, 10),
                ),
                churned,
            )

    def test_terminal_ticket_recheck_uses_access_policy_semantics(self) -> None:
        ticket_snapshot = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"ticket\n",
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=7,
            uid=os.geteuid(),
            gid=20,
        )
        target_expectation = MODULE.PendingRegularTargetExpectation(
            target=ROLE_TARGET,
            parent_identity=(5, 6),
            file_identity=(7, 8),
            sha256="a" * 64,
            size=10,
            mode=0o600,
            uid=os.geteuid(),
        )
        ticket = MODULE.PendingBatchCleanupTicket(
            version=4,
            phase="after",
            path=self.home / "ticket.json",
            snapshot=ticket_snapshot,
            batch_root=self.home / "batch",
            batch_root_identity=(9, 10),
            marker_path=MODULE.PENDING_STATE_COMMIT_MARKER,
            marker_parent_identity=(11, 12),
            marker_file_identity=(13, 14),
            marker_mode=0o600,
            marker_sha256="b" * 64,
            terminal_regular_targets=(target_expectation,),
        )
        churned_ticket = MODULE.replace(
            ticket,
            snapshot=MODULE.replace(ticket_snapshot, gid=80),
        )
        target_snapshot = MODULE.RegularFileSnapshot(
            parent_identity=target_expectation.parent_identity,
            file_identity=target_expectation.file_identity,
            sha256=target_expectation.sha256,
            size=target_expectation.size,
            mode=target_expectation.mode,
            uid=target_expectation.uid,
            gid=80,
            link_count=1,
        )
        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=churned_ticket,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                return_value=target_snapshot,
            ),
        ):
            MODULE._verify_final_regular_targets(self.home, ticket)

        changed_ticket = MODULE.replace(churned_ticket, marker_sha256="c" * 64)
        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=changed_ticket,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "cleanup ticket changed"),
        ):
            MODULE._verify_final_regular_targets(self.home, ticket)

    def test_stat_metadata_gid_comparison_follows_group_access(self) -> None:
        def metadata(mode: int, gid: int) -> SimpleNamespace:
            return SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_mode=stat.S_IFREG | mode,
                st_uid=501,
                st_gid=gid,
                st_size=10,
                st_nlink=1,
            )

        self.assertTrue(
            MODULE._regular_stat_metadata_matches(
                metadata(0o600, 80),
                metadata(0o600, 20),
            )
        )
        self.assertFalse(
            MODULE._regular_stat_metadata_matches(
                metadata(0o640, 80),
                metadata(0o640, 20),
            )
        )

    def test_managed_state_file_match_gid_follows_group_access(self) -> None:
        expected = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b'{"version": 1}\n',
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=15,
            uid=501,
            gid=20,
        )
        actual = MODULE.replace(expected, gid=80)
        target = self.home / "state" / "managed.json"

        with mock.patch.object(
            MODULE,
            "_read_managed_state_file_snapshot",
            return_value=actual,
        ):
            self.assertTrue(
                MODULE._managed_state_file_matches(
                    self.home,
                    target,
                    expected,
                    parent_fd=10,
                )
            )

        group_expected = MODULE.replace(expected, mode=0o640)
        group_actual = MODULE.replace(actual, mode=0o640)
        with mock.patch.object(
            MODULE,
            "_read_managed_state_file_snapshot",
            return_value=group_actual,
        ):
            self.assertFalse(
                MODULE._managed_state_file_matches(
                    self.home,
                    target,
                    group_expected,
                    parent_fd=10,
                )
            )

    def test_regular_file_snapshot_named_open_gid_churn_follows_group_access(
        self,
    ) -> None:
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                target = self.home / f"regular-snapshot-{mode:o}.toml"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('name = "reviewer"\n', encoding="utf-8")
                target.chmod(mode)
                original_gid = target.stat().st_gid
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != original_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
                real_open = MODULE.os.open
                churned = False

                def churn_gid_before_open(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    flags: int,
                    mode_bits: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal churned
                    if path == target.name and dir_fd == parent_fd and not churned:
                        os.chown(target, -1, alternate_gid)
                        churned = True
                    return real_open(path, flags, mode_bits, dir_fd=dir_fd)

                try:
                    with mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=churn_gid_before_open,
                    ):
                        if mode == 0o600:
                            snapshot = MODULE._regular_file_snapshot_at(
                                parent_fd,
                                target.name,
                                target,
                            )
                            self.assertEqual(snapshot.gid, original_gid)
                        else:
                            with self.assertRaisesRegex(
                                MODULE.SyncError,
                                "changed before read",
                            ):
                                MODULE._regular_file_snapshot_at(
                                    parent_fd,
                                    target.name,
                                    target,
                                )
                finally:
                    MODULE._close_fd_quietly(parent_fd)
                self.assertTrue(churned)
                self.assertEqual(target.stat().st_gid, alternate_gid)

    def test_managed_state_reader_named_open_gid_churn_follows_group_access(
        self,
    ) -> None:
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                target = self.home / "state" / f"managed-{mode:o}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"version": 1}\n', encoding="utf-8")
                target.chmod(mode)
                original_gid = target.stat().st_gid
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != original_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
                real_open = MODULE.os.open
                churned = False

                def churn_gid_before_open(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    flags: int,
                    mode_bits: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal churned
                    if path == target.name and dir_fd == parent_fd and not churned:
                        os.chown(target, -1, alternate_gid)
                        churned = True
                    return real_open(path, flags, mode_bits, dir_fd=dir_fd)

                try:
                    with mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=churn_gid_before_open,
                    ):
                        if mode == 0o600:
                            snapshot = MODULE._read_managed_state_file_snapshot(
                                self.home,
                                target,
                                parent_fd,
                            )
                            self.assertEqual(snapshot.gid, alternate_gid)
                        else:
                            with self.assertRaisesRegex(
                                MODULE.SyncError,
                                "changed before read",
                            ):
                                MODULE._read_managed_state_file_snapshot(
                                    self.home,
                                    target,
                                    parent_fd,
                                )
                finally:
                    MODULE._close_fd_quietly(parent_fd)
                self.assertTrue(churned)
                self.assertEqual(target.stat().st_gid, alternate_gid)

    def _retain_committed_batch(
        self,
        release: Path,
        *,
        sha: str = SHA_A,
    ) -> MODULE.PendingLinkBatch:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected committed pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_committed_pointer,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(release, self.home, sha)
        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        return batch

    def _rewrite_linked_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        mutate,
    ) -> None:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        mutate(payload)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_legacy_v6_writer_gid_recovers_committed_create_and_replace(
        self,
    ) -> None:
        for action in ("create", "replace"):
            with self.subTest(action=action):
                self.home = self.root / f"home-committed-v6-writer-{action}"
                release = self.root / f"release-committed-v6-writer-{action}"
                sha = SHA_A
                expected_payload = 'name = "reviewer"\n'
                if action == "replace":
                    initial = self.root / "release-committed-v6-writer-initial"
                    write_release(initial, role_payload='name = "initial"\n')
                    install(initial, self.home, SHA_A)
                    expected_payload = 'name = "updated"\n'
                    sha = SHA_B
                write_release(release, role_payload=expected_payload)

                batch = self._retain_committed_batch(release, sha=sha)
                payload, legacy_gid = legacy_v6_writer_metadata_payload(batch)
                assert legacy_gid is not None
                write_pending_metadata_payload(batch, payload)
                target = self.home / ROLE_TARGET
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != legacy_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                os.chown(target, -1, alternate_gid)

                parsed = MODULE._load_pending_link_batch(self.home)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                parsed_record = next(
                    record for record in parsed.records if record.is_regular()
                )
                self.assertEqual(parsed.metadata_version, 6)
                self.assertEqual(parsed_record.regular_gid, legacy_gid)
                self.assertEqual(target.stat().st_gid, alternate_gid)

                install(release, self.home, sha)

                self.assertEqual(target.read_text(encoding="utf-8"), expected_payload)
                self.assertEqual(target.stat().st_gid, alternate_gid)
                self.assertEqual(target.stat().st_nlink, 1)
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                )

    def test_legacy_v6_writer_remove_recovers_committed_finalization(self) -> None:
        initial = self.root / "release-committed-v6-writer-remove-initial"
        write_release(initial, role_payload='name = "reviewer"\n')
        install(initial, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        original_gid = target.stat().st_gid
        removal_release = self.root / "release-committed-v6-writer-remove"
        write_release(removal_release)

        batch = self._retain_committed_batch(removal_release, sha=SHA_B)
        payload, producing_gid = legacy_v6_writer_metadata_payload(batch)
        self.assertIsNone(producing_gid)
        write_pending_metadata_payload(batch, payload)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        self.assertEqual(parsed.metadata_version, 6)
        self.assertEqual(record.action, "remove")
        self.assertIsNone(record.regular_gid)
        self.assertEqual(record.planned_snapshot.regular_gid, original_gid)
        self.assertFalse(os.path.lexists(target))

        install(removal_release, self.home, SHA_B)

        self.assertFalse(os.path.lexists(target))
        self.assertNotIn(ROLE_TARGET, MODULE._load_managed_state(self.home).links)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_pending_regular_gid_version_validation(self) -> None:
        release = self.root / "release-regular-gid-validation"
        write_release(release, role_payload='name = "reviewer"\n')
        batch = self._retain_committed_batch(release)
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        current_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        legacy_payload, legacy_gid = legacy_v6_writer_metadata_payload(batch)
        assert legacy_gid is not None

        for invalid_gid in (True, "20", -1):
            with self.subTest(version=6, regular_gid=invalid_gid):
                payload = json.loads(json.dumps(legacy_payload))
                regular_record = next(
                    record
                    for record in payload["records"]
                    if record["materialization"] == "regular"
                )
                regular_record["regular_gid"] = invalid_gid
                write_pending_metadata_payload(batch, payload)
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "invalid regular-file evidence",
                ):
                    MODULE._load_pending_link_batch(self.home)

        for version in (7, 8):
            with self.subTest(version=version, regular_gid=legacy_gid):
                payload = json.loads(json.dumps(current_payload))
                payload["version"] = version
                downgrade_pending_state_evidence_metadata(payload, version)
                for raw_record in payload["records"]:
                    raw_record.pop("before_materialization")
                    raw_record.pop("removed_link")
                if version == 7:
                    for raw_record in payload["records"]:
                        raw_record.pop("publication_cleanup")
                regular_record = next(
                    record
                    for record in payload["records"]
                    if record["materialization"] == "regular"
                )
                regular_record["regular_gid"] = legacy_gid
                write_pending_metadata_payload(batch, payload)
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "invalid regular-file evidence",
                ):
                    MODULE._load_pending_link_batch(self.home)

    def test_v5_symlink_pending_metadata_remains_readable(self) -> None:
        release = self.root / "symlink-release"
        write_release(release)
        batch = self._retain_committed_batch(release)

        def downgrade(payload: dict[str, object]) -> None:
            payload["version"] = 5
            downgrade_pending_state_evidence_metadata(payload, 5)
            payload.pop("terminal_regular_before", None)
            payload.pop("terminal_regular_after", None)
            records = payload["records"]
            assert isinstance(records, list)
            for raw_record in records:
                assert isinstance(raw_record, dict)
                raw_record.pop("before_materialization")
                raw_record.pop("removed_link")
                raw_record.pop("publication_cleanup")
                for field in (
                    "materialization",
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    raw_record.pop(field)
                planned = raw_record["planned_before"]
                assert isinstance(planned, dict)
                for field in (
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    planned.pop(field)

        self._rewrite_linked_metadata(batch, downgrade)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(
            all(record.materialization == "symlink" for record in parsed.records)
        )

    def test_v6_pending_metadata_rejects_unknown_closed_field(self) -> None:
        release = self.root / "regular-release"
        write_release(release, role_payload='name = "reviewer"\n')
        batch = self._retain_committed_batch(release)
        payload, _legacy_gid = legacy_v6_writer_metadata_payload(batch)
        assert _legacy_gid is not None
        records = payload["records"]
        assert isinstance(records, list)
        record = records[-1]
        assert isinstance(record, dict)
        record["unexpected_regular_field"] = True
        write_pending_metadata_payload(batch, payload)

        with self.assertRaisesRegex(MODULE.SyncError, "record .* is invalid"):
            MODULE._load_pending_link_batch(self.home)

    def test_v8_terminal_regular_sets_cover_all_state_targets(self) -> None:
        initial = self.root / "initial-release"
        write_release(initial, role_payload='name = "reviewer"\n')
        install(initial, self.home, SHA_A)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "reviewer"\n')
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            next_release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )

        batch = self._retain_committed_batch(next_release, sha=SHA_B)

        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(
            metadata["version"],
            MODULE.PENDING_LINK_METADATA_VERSION,
        )
        self.assertEqual(
            [item["target"] for item in metadata["terminal_regular_before"]],
            [ROLE_TARGET.as_posix()],
        )
        self.assertEqual(
            [item["target"] for item in metadata["terminal_regular_after"]],
            [ROLE_TARGET.as_posix(), secondary_target.as_posix()],
        )
        self.assertEqual(
            tuple(item.target for item in batch.terminal_regular_before),
            (ROLE_TARGET,),
        )
        self.assertEqual(
            tuple(item.target for item in batch.terminal_regular_after),
            (ROLE_TARGET, secondary_target),
        )
        acted_regular_targets = {
            record.target
            for record in batch.records
            if record.is_regular()
            and record.action in {"create", "replace", "quarantine-replace"}
        }
        self.assertNotIn(ROLE_TARGET, acted_regular_targets)
        self.assertIn(secondary_target, acted_regular_targets)

    def test_v6_action_scoped_metadata_recovers_unchanged_regular_target(
        self,
    ) -> None:
        initial = self.root / "initial-release"
        write_release(initial, role_payload='name = "reviewer"\n')
        install(initial, self.home, SHA_A)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "reviewer"\n')
        append_regular_link(
            next_release,
            target="agents/security-reviewer.toml",
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )
        batch = self._retain_committed_batch(next_release, sha=SHA_B)
        legacy_payload, _legacy_gid = legacy_v6_writer_metadata_payload(batch)
        assert _legacy_gid is not None
        write_pending_metadata_payload(batch, legacy_payload)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, 6)
        self.assertNotIn(
            ROLE_TARGET,
            {
                record.target
                for record in parsed.records
                if record.is_regular()
                and record.action in {"create", "replace", "quarantine-replace"}
            },
        )

        install(next_release, self.home, SHA_B)

        self.assertEqual(
            (self.home / ROLE_TARGET).read_text(encoding="utf-8"),
            'name = "reviewer"\n',
        )
        self.assertEqual(
            (self.home / "agents" / "security-reviewer.toml").read_text(
                encoding="utf-8"
            ),
            'name = "security-reviewer"\n',
        )


class PendingRegularSourceEvidenceCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _retain_committed_batch(
        self,
        release: Path,
        *,
        sha: str = SHA_A,
    ) -> MODULE.PendingLinkBatch:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected committed pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_committed_pointer,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(release, self.home, sha)
        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        return batch

    def _installed_release_root(self, sha: str = SHA_A) -> Path:
        return MODULE._releases_root(self.home, MODULE.PUBLIC_OWNER) / sha

    def _replace_installed_source(
        self,
        source: Path,
        payload: str,
    ) -> None:
        mode = stat.S_IMODE(source.stat().st_mode)
        replacement = source.with_name(f".{source.name}.replacement")
        replacement.write_text(payload, encoding="utf-8")
        replacement.chmod(mode)
        os.replace(replacement, source)

    def _mutate_installed_source(self, source: Path, payload: str) -> None:
        mode = stat.S_IMODE(source.stat().st_mode)
        source.chmod(mode | stat.S_IWUSR)
        source.write_text(payload, encoding="utf-8")
        source.chmod(mode)

    def test_shared_release_is_captured_once_and_finally_revalidated_once(
        self,
    ) -> None:
        release = self.root / "shared-release"
        write_release(release, role_payload='name = "reviewer"\n')
        append_regular_link(
            release,
            target="agents/reviewer-copy.toml",
        )
        append_regular_link(
            release,
            target="agents/security-reviewer.toml",
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )
        self._retain_committed_batch(release)

        with (
            mock.patch.object(
                MODULE,
                "_capture_pending_regular_release_receipt",
                wraps=MODULE._capture_pending_regular_release_receipt,
            ) as capture_release,
            mock.patch.object(
                MODULE,
                "_require_pending_regular_release_receipt",
                wraps=MODULE._require_pending_regular_release_receipt,
            ) as revalidate_release,
        ):
            parsed = MODULE._load_pending_link_batch(self.home)

        self.assertIsNotNone(parsed)
        self.assertEqual(capture_release.call_count, 1)
        self.assertEqual(revalidate_release.call_count, 1)

    def test_release_mutation_after_first_evidence_fails_final_revalidation(
        self,
    ) -> None:
        release = self.root / "mutated-release"
        write_release(release, role_payload='name = "reviewer"\n')
        self._retain_committed_batch(release)
        source = (
            self._installed_release_root()
            / "personal_codex"
            / "agents"
            / "reviewer.toml"
        )
        real_evidence = MODULE._PendingRegularSourceEvidenceBudget.evidence
        mutated = False

        def mutate_after_first_evidence(
            budget: MODULE._PendingRegularSourceEvidenceBudget,
            home: Path,
            record: MODULE.ManagedLinkRecord,
            expectation: MODULE.PendingReleaseExpectation,
        ) -> MODULE._PendingRegularSourceEvidence:
            nonlocal mutated
            evidence = real_evidence(budget, home, record, expectation)
            if not mutated:
                self._mutate_installed_source(source, 'name = "mutated"\n')
                mutated = True
            return evidence

        with (
            mock.patch.object(
                MODULE._PendingRegularSourceEvidenceBudget,
                "evidence",
                new=mutate_after_first_evidence,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "source evidence receipt no longer matches",
            ),
        ):
            MODULE._load_pending_link_batch(self.home)
        self.assertTrue(mutated)

    def test_source_replacement_is_rejected_on_source_cache_hit(self) -> None:
        release = self.root / "source-cache-hit-release"
        write_release(release, role_payload='name = "reviewer"\n')
        append_regular_link(
            release,
            target="agents/reviewer-copy.toml",
        )
        self._retain_committed_batch(release)
        source = (
            self._installed_release_root()
            / "personal_codex"
            / "agents"
            / "reviewer.toml"
        )
        real_evidence = MODULE._PendingRegularSourceEvidenceBudget.evidence
        replaced = False

        def replace_after_first_evidence(
            budget: MODULE._PendingRegularSourceEvidenceBudget,
            home: Path,
            record: MODULE.ManagedLinkRecord,
            expectation: MODULE.PendingReleaseExpectation,
        ) -> MODULE._PendingRegularSourceEvidence:
            nonlocal replaced
            evidence = real_evidence(budget, home, record, expectation)
            if not replaced:
                self._replace_installed_source(source, 'name = "replacement"\n')
                replaced = True
            return evidence

        with (
            mock.patch.object(
                MODULE._PendingRegularSourceEvidenceBudget,
                "evidence",
                new=replace_after_first_evidence,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "source evidence receipt no longer matches",
            ),
        ):
            MODULE._load_pending_link_batch(self.home)
        self.assertTrue(replaced)

    def test_release_directory_replacement_before_finalization_is_rejected(
        self,
    ) -> None:
        release = self.root / "release-directory-replacement"
        write_release(release, role_payload='name = "reviewer"\n')
        self._retain_committed_batch(release)
        release_root = self._installed_release_root()
        parked_root = release_root.with_name(f"{release_root.name}.parked")
        real_evidence = MODULE._PendingRegularSourceEvidenceBudget.evidence
        replaced = False

        def replace_after_first_evidence(
            budget: MODULE._PendingRegularSourceEvidenceBudget,
            home: Path,
            record: MODULE.ManagedLinkRecord,
            expectation: MODULE.PendingReleaseExpectation,
        ) -> MODULE._PendingRegularSourceEvidence:
            nonlocal replaced
            evidence = real_evidence(budget, home, record, expectation)
            if not replaced:
                release_root.rename(parked_root)
                shutil.copytree(parked_root, release_root)
                replaced = True
            return evidence

        with (
            mock.patch.object(
                MODULE._PendingRegularSourceEvidenceBudget,
                "evidence",
                new=replace_after_first_evidence,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "source release directory changed",
            ),
        ):
            MODULE._load_pending_link_batch(self.home)
        self.assertTrue(replaced)

    def test_finalized_budget_rejects_later_evidence(self) -> None:
        budget = MODULE._PendingRegularSourceEvidenceBudget()
        budget.finalize(self.home)

        with self.assertRaisesRegex(MODULE.SyncError, "evidence budget is sealed"):
            budget.evidence(
                self.home,
                SimpleNamespace(),
                SimpleNamespace(),
            )


if __name__ == "__main__":
    unittest.main()
