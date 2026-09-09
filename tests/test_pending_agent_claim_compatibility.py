from __future__ import annotations

import contextlib
from dataclasses import replace
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
    "codex_personal_sync_pending_agent_claim_compatibility",
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
UNCHANGED_ROLE_TARGET = PurePosixPath("agents/worker.toml")


def write_agent_release(
    root: Path,
    *,
    payload: str = 'name = "reviewer"\n',
    unchanged_payload: str | None = None,
    owner: str = MODULE.PUBLIC_OWNER,
    override: bool = False,
    base_sha: str | None = None,
) -> None:
    source = root / "personal_codex" / "agents" / "reviewer.toml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(payload, encoding="utf-8")
    links: list[dict[str, object]] = [
        {
            "source": "personal_codex/agents/reviewer.toml",
            "target": ROLE_TARGET.as_posix(),
            "kind": "file",
            "owner": owner,
        }
    ]
    if override:
        links[0]["override"] = True
    if unchanged_payload is not None:
        unchanged_source = root / "personal_codex" / "agents" / "worker.toml"
        unchanged_source.write_text(unchanged_payload, encoding="utf-8")
        links.append(
            {
                "source": "personal_codex/agents/worker.toml",
                "target": UNCHANGED_ROLE_TARGET.as_posix(),
                "kind": "file",
                "owner": owner,
            }
        )
    manifest_payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": links,
    }
    if base_sha is not None:
        manifest_payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_sha,
        }
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_payload) + "\n", encoding="utf-8")


def write_agent_source_batch(
    root: Path,
    sources_by_target: dict[PurePosixPath, tuple[PurePosixPath, str]],
) -> None:
    links: list[dict[str, object]] = []
    payloads_by_source: dict[PurePosixPath, str] = {}
    for target, (source, payload) in sources_by_target.items():
        existing_payload = payloads_by_source.setdefault(source, payload)
        if existing_payload != payload:
            raise AssertionError(f"conflicting fixture payload for {source}")
        links.append(
            {
                "source": source.as_posix(),
                "target": target.as_posix(),
                "kind": "file",
                "owner": MODULE.PUBLIC_OWNER,
            }
        )
    for source, payload in payloads_by_source.items():
        source_path = root / Path(*source.parts)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(payload, encoding="utf-8")
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": MODULE.PUBLIC_OWNER,
                "links": links,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def write_agent_to_symlink_batch(
    root: Path,
    prior_sources_by_target: dict[PurePosixPath, PurePosixPath],
) -> None:
    links: list[dict[str, object]] = []
    removed_links: list[dict[str, object]] = []
    for index, (target, prior_source) in enumerate(prior_sources_by_target.items()):
        replacement_source = PurePosixPath(
            "personal_codex",
            "skills",
            f"replacement-{index}",
        )
        skill = root / Path(*replacement_source.parts) / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"# Replacement {index}\n", encoding="utf-8")
        links.append(
            {
                "source": replacement_source.as_posix(),
                "target": target.as_posix(),
                "kind": "skill",
                "owner": MODULE.PUBLIC_OWNER,
            }
        )
        removed_links.append(
            {
                "id": f"regular-agent-to-symlink-{index}",
                "source": prior_source.as_posix(),
                "target": target.as_posix(),
                "kind": "file",
                "replacement_target": target.as_posix(),
            }
        )
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": MODULE.PUBLIC_OWNER,
                "links": links,
                "removed_links": removed_links,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def install(root: Path, home: Path, sha: str = SHA_A) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, sha, dry_run=False)


class PendingAgentClaimCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _retain_batch(
        self,
        home: Path,
        release: Path,
        *,
        committed: bool,
        legacy_symlink: bool,
        sha: str = SHA_A,
    ) -> MODULE.PendingLinkBatch:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_pointer(
            selected_home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            retained_phase = "after" if committed else "before"
            if phase == retained_phase:
                raise MODULE.SyncError("injected pending pointer retention")
            real_clear(selected_home, batch, phase=phase)

        expected_error = (
            "committed managed state but finalization failed"
            if committed
            else "rollback was incomplete"
        )
        with contextlib.ExitStack() as stack:
            if legacy_symlink:
                stack.enter_context(
                    mock.patch.object(
                        MODULE,
                        "_entry_materializes_regular_file",
                        return_value=False,
                    )
                )
            if not committed:
                stack.enter_context(
                    mock.patch.object(
                        MODULE,
                        "_publish_pending_commit_marker",
                        side_effect=MODULE.SyncError("injected precommit crash"),
                    )
                )
            stack.enter_context(
                mock.patch.object(
                    MODULE,
                    "_clear_pending_link_pointer",
                    side_effect=retain_pointer,
                )
            )
            with self.assertRaisesRegex(MODULE.SyncError, expected_error):
                install(release, home, sha)
            batch = MODULE._load_pending_link_batch(home)
            self.assertIsNotNone(batch)
            assert batch is not None
            return batch

    def _downgrade_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        version: int,
        *,
        preserve_v6_null_gid: bool = False,
    ) -> None:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["version"] = version
        if version < 10:
            for raw_record in payload["records"]:
                raw_record.pop("before_materialization", None)
                raw_record.pop("removed_link", None)
        if version < 7:
            payload.pop("terminal_regular_before")
            payload.pop("terminal_regular_after")
        if version < 8:
            for raw_record in payload["records"]:
                raw_record.pop("publication_cleanup", None)
        if version < 9:
            for field in ("state_before", "state_after", "commit_evidence"):
                payload[field].pop("uid")
                payload[field].pop("gid")
        if version == 6 and not preserve_v6_null_gid:
            for raw_record in payload["records"]:
                if raw_record["materialization"] == "regular" and raw_record[
                    "action"
                ] in {"create", "replace", "quarantine-replace"}:
                    stage = raw_record["stage"]
                    assert isinstance(stage, str)
                    stage_metadata = os.stat(batch.batch_root / stage)
                    assert raw_record["regular_gid"] is None
                    raw_record["regular_gid"] = stage_metadata.st_gid
        if version < 6:
            for raw_record in payload["records"]:
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
                for field in (
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    planned.pop(field)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _drop_cleanup_index(self, home: Path) -> None:
        index = MODULE._pending_cleanup_index_path(home)
        for child in index.iterdir():
            child.unlink()
        index.rmdir()

    def _retain_batch_with_state_before(
        self,
        label: str,
        *,
        committed: bool,
    ) -> tuple[Path, MODULE.PendingLinkBatch]:
        home = self.root / f"home-state-before-{label}"
        first_release = self.root / f"first-state-before-{label}"
        next_release = self.root / f"next-state-before-{label}"
        write_agent_release(first_release, payload='name = "first"\n')
        write_agent_release(next_release, payload='name = "next"\n')
        install(first_release, home, SHA_A)
        batch = self._retain_batch(
            home,
            next_release,
            committed=committed,
            legacy_symlink=False,
            sha=SHA_B,
        )
        self.assertTrue(batch.state_before.exists)
        self.assertIsNotNone(batch.state_before_evidence)
        return home, batch

    def _terminal_regular_budget_fixture(
        self,
        label: str,
    ) -> tuple[Path, MODULE.ManagedState, int]:
        home = self.root / f"home-terminal-budget-{label}"
        reviewer_payload = 'name = "reviewer-budget"\n'
        worker_payload = 'name = "worker-budget"\n'
        sources = {
            ROLE_TARGET: (
                PurePosixPath("personal_codex/agents/reviewer.toml"),
                reviewer_payload,
            ),
            UNCHANGED_ROLE_TARGET: (
                PurePosixPath("personal_codex/agents/worker.toml"),
                worker_payload,
            ),
        }
        links: dict[PurePosixPath, MODULE.ManagedLinkRecord] = {}
        for target, (source, payload) in sources.items():
            release_source = (
                MODULE._releases_root(home, MODULE.PUBLIC_OWNER)
                / SHA_A
                / Path(*source.parts)
            )
            release_source.parent.mkdir(parents=True, exist_ok=True)
            release_source.write_text(payload, encoding="utf-8")
            installed_target = home / Path(*target.parts)
            installed_target.parent.mkdir(parents=True, exist_ok=True)
            installed_target.write_text(payload, encoding="utf-8")
            installed_target.chmod(0o600)
            links[target] = MODULE.ManagedLinkRecord(
                source=source,
                target=target,
                kind="file",
                owner=MODULE.PUBLIC_OWNER,
                link_target=MODULE._relative_managed_link_target(
                    source,
                    target,
                    MODULE.PUBLIC_OWNER,
                ),
                release_sha=SHA_A,
            )
        return (
            home,
            MODULE.ManagedState(
                owners={MODULE.PUBLIC_OWNER: SHA_A},
                links=links,
            ),
            len(reviewer_payload.encode("utf-8")) + len(worker_payload.encode("utf-8")),
        )

    def test_v4_v5_agent_symlink_claims_parse_and_recover(self) -> None:
        for version in (4, 5):
            for committed in (False, True):
                with self.subTest(version=version, committed=committed):
                    home = self.root / f"home-v{version}-{committed}"
                    release = self.root / f"release-v{version}-{committed}"
                    write_agent_release(release)
                    batch = self._retain_batch(
                        home,
                        release,
                        committed=committed,
                        legacy_symlink=True,
                    )
                    self._downgrade_metadata(batch, version)

                    parsed = MODULE._load_pending_link_batch(home)
                    self.assertIsNotNone(parsed)
                    assert parsed is not None
                    self.assertEqual(parsed.metadata_version, version)
                    self.assertIn(
                        ROLE_TARGET,
                        {
                            claim.target
                            for claim in parsed.claims_after
                            if claim.scope == "managed"
                        },
                    )

                    state, snapshot = MODULE._load_managed_state_with_snapshot(home)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _state, _snapshot, recovered = (
                            MODULE._recover_pending_link_transaction(
                                home,
                                state,
                                snapshot,
                                dry_run=False,
                            )
                        )
                    self.assertTrue(recovered)
                    self.assertFalse(
                        os.path.lexists(MODULE._pending_link_pointer_path(home))
                    )
                    target = home / ROLE_TARGET
                    self.assertEqual(target.is_symlink(), committed)

    def test_v6_omits_regular_agent_claim_and_rejects_legacy_extra_claim(
        self,
    ) -> None:
        home = self.root / "home-v6"
        release = self.root / "release-v6"
        write_agent_release(release)
        batch = self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._downgrade_metadata(batch, 6)

        parsed = MODULE._load_pending_link_batch(home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, 6)
        self.assertNotIn(
            ROLE_TARGET,
            {claim.target for claim in parsed.claims_after if claim.scope == "managed"},
        )

        state_record = parsed.state_after_value.links[ROLE_TARGET]
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        claims_after = payload["claims_after"]
        claims_after.append(
            {
                "index": len(claims_after),
                "scope": "managed",
                "target": ROLE_TARGET.as_posix(),
                "kind": state_record.kind,
                "source": state_record.source.as_posix(),
                "owner": state_record.owner,
                "link_target": state_record.link_target,
                "release_sha": state_record.release_sha,
                "parent_identity": [0, 0],
                "link_identity": [0, 0],
                "evidence": (f"pending/claims/after/{len(claims_after) - 1:08d}"),
            }
        )
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "after claims do not exactly match state",
        ):
            MODULE._load_pending_link_batch(home)

    def test_v6_historical_null_gid_regular_records_recover(self) -> None:
        for committed in (False, True):
            with self.subTest(committed=committed):
                home = self.root / f"home-v6-null-gid-{committed}"
                release = self.root / f"release-v6-null-gid-{committed}"
                write_agent_release(release)
                batch = self._retain_batch(
                    home,
                    release,
                    committed=committed,
                    legacy_symlink=False,
                )
                self._downgrade_metadata(
                    batch,
                    6,
                    preserve_v6_null_gid=True,
                )

                metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                producing_regular = [
                    raw_record
                    for raw_record in payload["records"]
                    if raw_record["materialization"] == "regular"
                    and raw_record["action"]
                    in {"create", "replace", "quarantine-replace"}
                ]
                self.assertTrue(producing_regular)
                self.assertTrue(
                    all(
                        raw_record["regular_gid"] is None
                        for raw_record in producing_regular
                    )
                )

                parsed = MODULE._load_pending_link_batch(home)
                self.assertIsNotNone(parsed)
                state, snapshot = MODULE._load_managed_state_with_snapshot(home)
                with contextlib.redirect_stdout(io.StringIO()):
                    _state, _snapshot, recovered = (
                        MODULE._recover_pending_link_transaction(
                            home,
                            state,
                            snapshot,
                            dry_run=False,
                        )
                    )

                self.assertTrue(recovered)
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(home))
                )
                self.assertEqual((home / ROLE_TARGET).is_file(), committed)

    def test_v7_v8_reject_integer_regular_gid(self) -> None:
        for version in (7, 8):
            with self.subTest(version=version):
                home = self.root / f"home-v{version}-integer-gid"
                release = self.root / f"release-v{version}-integer-gid"
                write_agent_release(release)
                batch = self._retain_batch(
                    home,
                    release,
                    committed=True,
                    legacy_symlink=False,
                )
                self._downgrade_metadata(batch, version)
                metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                raw_record = next(
                    raw_record
                    for raw_record in payload["records"]
                    if raw_record["materialization"] == "regular"
                    and raw_record["action"]
                    in {"create", "replace", "quarantine-replace"}
                )
                assert raw_record["regular_gid"] is None
                stage = raw_record["stage"]
                assert isinstance(stage, str)
                raw_record["regular_gid"] = os.stat(batch.batch_root / stage).st_gid
                metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "invalid regular-file evidence",
                ):
                    MODULE._load_pending_link_batch(home)

    def test_v9_managed_state_evidence_binds_uid_and_gid_policy(self) -> None:
        home = self.root / "home-v9-state-ownership"
        release = self.root / "release-v9-state-ownership"
        write_agent_release(release)
        batch = self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._downgrade_metadata(batch, 9)
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))

        self.assertEqual(payload["version"], 9)
        self.assertIsNone(payload["state_before"]["uid"])
        self.assertIsNone(payload["state_before"]["gid"])
        for field in ("state_after", "commit_evidence"):
            raw = payload[field]
            evidence = raw["evidence"]
            assert isinstance(evidence, str)
            evidence_stat = os.stat(batch.batch_root / evidence)
            self.assertEqual(raw["uid"], evidence_stat.st_uid)
            self.assertEqual(raw["gid"], evidence_stat.st_gid)

        raw_after = payload["state_after"]
        changed_uid = dict(raw_after)
        changed_uid["uid"] += 1
        with self.assertRaisesRegex(MODULE.SyncError, "evidence changed"):
            MODULE._read_pending_state_evidence(
                home,
                batch.batch_root,
                changed_uid,
                label="state-after",
                state_parent_identity=batch.state_after.parent_identity,
                require_exists=True,
                required_mode=0o600,
                metadata_version=9,
            )

        private_gid_drift = dict(raw_after)
        private_gid_drift["gid"] += 1
        snapshot, _evidence = MODULE._read_pending_state_evidence(
            home,
            batch.batch_root,
            private_gid_drift,
            label="state-after",
            state_parent_identity=batch.state_after.parent_identity,
            require_exists=True,
            required_mode=0o600,
            metadata_version=9,
        )
        self.assertEqual(snapshot.file_identity, batch.state_after.file_identity)
        assert batch.state_after.uid is not None
        assert batch.state_after.gid is not None
        self.assertFalse(
            MODULE._managed_state_snapshot_exact(
                replace(batch.state_after, uid=batch.state_after.uid + 1),
                batch.state_after,
            )
        )
        self.assertTrue(
            MODULE._managed_state_snapshot_exact(
                replace(batch.state_after, gid=batch.state_after.gid + 1),
                batch.state_after,
            )
        )
        group_bound = replace(batch.state_after, mode=0o640)
        self.assertFalse(
            MODULE._managed_state_snapshot_exact(
                replace(group_bound, gid=batch.state_after.gid + 1),
                group_bound,
            )
        )

        evidence = raw_after["evidence"]
        assert isinstance(evidence, str)
        evidence_path = batch.batch_root / evidence
        evidence_path.chmod(0o640)
        group_bearing = dict(raw_after)
        group_bearing["mode"] = 0o640
        MODULE._read_pending_state_evidence(
            home,
            batch.batch_root,
            group_bearing,
            label="state-after",
            state_parent_identity=batch.state_after.parent_identity,
            require_exists=True,
            required_mode=None,
            metadata_version=9,
        )
        changed_group = dict(group_bearing)
        changed_group["gid"] += 1
        with self.assertRaisesRegex(MODULE.SyncError, "evidence changed"):
            MODULE._read_pending_state_evidence(
                home,
                batch.batch_root,
                changed_group,
                label="state-after",
                state_parent_identity=batch.state_after.parent_identity,
                require_exists=True,
                required_mode=None,
                metadata_version=9,
            )

        legacy_group_bearing = dict(group_bearing)
        legacy_group_bearing.pop("uid")
        legacy_group_bearing.pop("gid")
        with self.assertRaisesRegex(MODULE.SyncError, "file metadata is invalid"):
            MODULE._read_pending_state_evidence(
                home,
                batch.batch_root,
                legacy_group_bearing,
                label="state-after",
                state_parent_identity=batch.state_after.parent_identity,
                require_exists=True,
                required_mode=None,
                metadata_version=8,
            )

    def test_state_before_restore_revalidates_uid_before_link(self) -> None:
        home, batch = self._retain_batch_with_state_before(
            "uid-drift",
            committed=True,
        )
        assert batch.state_before_evidence is not None
        evidence_path = batch.batch_root / Path(*batch.state_before_evidence.parts)
        state_path = MODULE._state_path(home)
        state_path.unlink()
        real_read = MODULE._read_managed_state_file_snapshot

        def read_with_foreign_owner(
            selected_home: Path,
            path: Path,
            parent_fd: int,
            **kwargs,
        ):
            snapshot = real_read(selected_home, path, parent_fd, **kwargs)
            if path == evidence_path:
                assert snapshot.uid is not None
                return replace(snapshot, uid=snapshot.uid + 1)
            return snapshot

        with (
            mock.patch.object(
                MODULE,
                "_read_managed_state_file_snapshot",
                side_effect=read_with_foreign_owner,
            ),
            mock.patch.object(MODULE.os, "link", wraps=MODULE.os.link) as link,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "state-before evidence changed",
            ),
        ):
            MODULE._restore_pending_state_before(home, batch)
        link.assert_not_called()
        self.assertFalse(state_path.exists())

    def test_state_before_restore_uses_gid_only_for_group_access(self) -> None:
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != os.stat(self.root).st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")

        for group_access in (False, True):
            with self.subTest(group_access=group_access):
                home, batch = self._retain_batch_with_state_before(
                    f"gid-drift-{group_access}",
                    committed=True,
                )
                assert batch.state_before_evidence is not None
                evidence_path = batch.batch_root / Path(
                    *batch.state_before_evidence.parts
                )
                state_path = MODULE._state_path(home)
                state_path.unlink()
                expected_mode = 0o640 if group_access else 0o600
                evidence_path.chmod(expected_mode)
                initial_gid = evidence_path.stat().st_gid
                if alternate_gid == initial_gid:
                    self.skipTest("alternate supplementary group matches evidence")
                expected = replace(
                    batch.state_before,
                    mode=expected_mode,
                    gid=initial_gid,
                )
                drifted_batch = replace(batch, state_before=expected)
                os.chown(evidence_path, -1, alternate_gid)

                if group_access:
                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "state-before evidence changed",
                    ):
                        MODULE._restore_pending_state_before(home, drifted_batch)
                    self.assertFalse(state_path.exists())
                else:
                    MODULE._restore_pending_state_before(home, drifted_batch)
                    self.assertTrue(state_path.is_file())

    def test_commit_and_rollback_markers_reject_foreign_owner_uid(self) -> None:
        commit_home, committed = self._retain_batch_with_state_before(
            "commit-owner",
            committed=True,
        )
        rollback_home, rolled_back = self._retain_batch_with_state_before(
            "rollback-owner",
            committed=False,
        )
        MODULE._publish_pending_rollback_marker(rollback_home, rolled_back)
        foreign_uid = os.geteuid() + 1

        with mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "commit evidence changed",
            ):
                MODULE._pending_commit_marker_snapshot(commit_home, committed)
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback marker changed",
            ):
                MODULE._pending_rollback_marker_snapshot(
                    rollback_home,
                    rolled_back,
                )

    def test_legacy_agent_symlink_migrates_to_regular_on_update(self) -> None:
        home = self.root / "home-legacy-update"
        first = self.root / "release-legacy-update-a"
        second = self.root / "release-legacy-update-b"
        write_agent_release(first, payload='name = "legacy"\n')
        write_agent_release(second, payload='name = "regular"\n')

        with mock.patch.object(
            MODULE,
            "_entry_materializes_regular_file",
            return_value=False,
        ):
            install(first, home, SHA_A)
        self.assertTrue((home / ROLE_TARGET).is_symlink())

        install(second, home, SHA_B)

        target = home / ROLE_TARGET
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "regular"\n')

    def test_legacy_overlay_symlink_migrates_to_regular_on_uninstall(self) -> None:
        home = self.root / "home-legacy-uninstall"
        public = self.root / "release-legacy-uninstall-public"
        private = self.root / "release-legacy-uninstall-private"
        write_agent_release(public, payload='provider = "public"\n')
        write_agent_release(
            private,
            payload='provider = "private"\n',
            owner="private",
            override=True,
            base_sha=SHA_A,
        )

        with mock.patch.object(
            MODULE,
            "_entry_materializes_regular_file",
            return_value=False,
        ):
            install(public, home, SHA_A)
            install(private, home, SHA_B)
        self.assertTrue((home / ROLE_TARGET).is_symlink())

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(home, "private", dry_run=False)

        target = home / ROLE_TARGET
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), 'provider = "public"\n')

    def test_v6_terminal_regular_authority_is_action_scoped(self) -> None:
        home = self.root / "home-v6-action-scoped"
        first = self.root / "release-v6-action-scoped-a"
        second = self.root / "release-v6-action-scoped-b"
        unchanged_payload = 'name = "worker"\n'
        write_agent_release(
            first,
            payload='name = "reviewer-a"\n',
            unchanged_payload=unchanged_payload,
        )
        write_agent_release(
            second,
            payload='name = "reviewer-b"\n',
            unchanged_payload=unchanged_payload,
        )
        install(first, home, SHA_A)
        batch = self._retain_batch(
            home,
            second,
            committed=True,
            legacy_symlink=False,
            sha=SHA_B,
        )
        self._downgrade_metadata(batch, 6)

        parsed = MODULE._load_pending_link_batch(home)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            {target.target for target in parsed.terminal_regular_before},
            {ROLE_TARGET},
        )
        self.assertEqual(
            {target.target for target in parsed.terminal_regular_after},
            {ROLE_TARGET},
        )

    def test_terminal_regular_staging_preflights_each_whole_phase_group(
        self,
    ) -> None:
        home, state, group_size = self._terminal_regular_budget_fixture("stage")

        for phase in ("before", "after"):
            with self.subTest(phase=phase, limit="L-1"):
                with (
                    mock.patch.object(
                        MODULE,
                        "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                        group_size - 1,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_regular_file_snapshot_beneath",
                        wraps=MODULE._read_regular_file_snapshot_beneath,
                    ) as read_snapshot,
                    mock.patch.object(
                        MODULE,
                        "_read_regular_source_payload",
                        wraps=MODULE._read_regular_source_payload,
                    ) as read_source,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "terminal regular.*aggregate size limit",
                    ),
                ):
                    MODULE._stage_pending_terminal_regular_targets(
                        home,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(read_snapshot.call_count, 0)
                self.assertEqual(read_source.call_count, 0)

            with self.subTest(phase=phase, limit="L"):
                with mock.patch.object(
                    MODULE,
                    "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                    group_size,
                ):
                    expectations = MODULE._stage_pending_terminal_regular_targets(
                        home,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(
                    sum(expectation.size for expectation in expectations),
                    group_size,
                )

    def test_terminal_regular_source_validation_preflights_whole_group(
        self,
    ) -> None:
        home, state, group_size = self._terminal_regular_budget_fixture("parse")

        for phase in ("before", "after"):
            expectations = MODULE._stage_pending_terminal_regular_targets(
                home,
                state,
                (),
                phase=phase,
            )
            with self.subTest(phase=phase, limit="L-1"):
                with (
                    mock.patch.object(
                        MODULE,
                        "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                        group_size - 1,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_regular_source_payload",
                        wraps=MODULE._read_regular_source_payload,
                    ) as read_source,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "terminal regular.*aggregate size limit",
                    ),
                ):
                    MODULE._validate_pending_terminal_regular_targets_for_state(
                        home,
                        expectations,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(read_source.call_count, 0)

            with self.subTest(phase=phase, limit="L"):
                with (
                    mock.patch.object(
                        MODULE,
                        "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                        group_size,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_regular_source_payload",
                        wraps=MODULE._read_regular_source_payload,
                    ) as read_source,
                ):
                    MODULE._validate_pending_terminal_regular_targets_for_state(
                        home,
                        expectations,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(read_source.call_count, len(expectations))

    def test_v10_parser_reads_shared_immutable_source_once(self) -> None:
        home = self.root / "home-v10-shared-source"
        first = self.root / "release-v10-shared-source-a"
        second = self.root / "release-v10-shared-source-b"
        shared_source = PurePosixPath("personal_codex/agents/shared.toml")
        shared_payload = 'name = "shared"\n'
        sources_by_target = {
            ROLE_TARGET: (shared_source, shared_payload),
            UNCHANGED_ROLE_TARGET: (shared_source, shared_payload),
        }
        write_agent_source_batch(first, sources_by_target)
        write_agent_to_symlink_batch(
            second,
            {
                target: source
                for target, (source, _payload) in sources_by_target.items()
            },
        )
        install(first, home, SHA_A)
        batch = self._retain_batch(
            home,
            second,
            committed=True,
            legacy_symlink=False,
            sha=SHA_B,
        )

        with mock.patch.object(
            MODULE,
            "_read_pending_regular_source_payload",
            wraps=MODULE._read_pending_regular_source_payload,
        ) as read_source:
            parsed = MODULE._load_pending_link_batch(home)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.metadata_version, 10)
        self.assertEqual(
            sum(
                record.before_is_regular() and not record.is_regular()
                for record in batch.records
            ),
            2,
        )
        self.assertEqual(read_source.call_count, 1)

    def test_pending_regular_source_cache_revalidates_source_receipt(self) -> None:
        home = self.root / "home-v10-source-cache-revalidation"
        release = self.root / "release-v10-source-cache-revalidation"
        source = PurePosixPath("personal_codex/agents/reviewer.toml")
        original_payload = 'name = "reviewer-a"\n'
        replacement_payload = 'name = "reviewer-b"\n'
        self.assertEqual(len(original_payload), len(replacement_payload))
        write_agent_source_batch(
            release,
            {ROLE_TARGET: (source, original_payload)},
        )
        install(release, home, SHA_A)
        state = MODULE._load_managed_state(home)
        record = state.links[ROLE_TARGET]
        identity, directory_identity = (
            MODULE._installed_release_identity_and_directory_identity(
                home,
                MODULE.PUBLIC_OWNER,
                SHA_A,
            )
        )
        expectation = MODULE.PendingReleaseExpectation(
            owner=MODULE.PUBLIC_OWNER,
            sha=SHA_A,
            directory_identity=directory_identity,
            tree_sha256=identity[2],
        )
        budget = MODULE._PendingRegularSourceEvidenceBudget()

        with mock.patch.object(
            MODULE,
            "_read_pending_regular_source_payload",
            wraps=MODULE._read_pending_regular_source_payload,
        ) as read_source:
            first = budget.evidence(home, record, expectation)
            installed_source = MODULE._record_regular_source_path(home, record)
            replacement = installed_source.with_name("replacement.toml")
            replacement.write_text(replacement_payload, encoding="utf-8")
            replacement.chmod(installed_source.stat().st_mode & 0o777)
            os.replace(replacement, installed_source)

            with self.assertRaisesRegex(
                MODULE.SyncError,
                "source evidence receipt no longer matches",
            ):
                budget.evidence(home, record, expectation)

        self.assertEqual(first.size, len(original_payload.encode("utf-8")))
        self.assertEqual(read_source.call_count, 1)
        self.assertEqual(budget.evidence_read_bytes, first.size)

    def test_pending_regular_source_cache_defers_release_tree_revalidation_until_finalize(
        self,
    ) -> None:
        home = self.root / "home-v10-release-cache-revalidation"
        release = self.root / "release-v10-release-cache-revalidation"
        source = PurePosixPath("personal_codex/agents/reviewer.toml")
        payload = 'name = "reviewer"\n'
        write_agent_source_batch(release, {ROLE_TARGET: (source, payload)})
        install(release, home, SHA_A)
        state = MODULE._load_managed_state(home)
        record = state.links[ROLE_TARGET]
        identity, directory_identity = (
            MODULE._installed_release_identity_and_directory_identity(
                home,
                MODULE.PUBLIC_OWNER,
                SHA_A,
            )
        )
        expectation = MODULE.PendingReleaseExpectation(
            owner=MODULE.PUBLIC_OWNER,
            sha=SHA_A,
            directory_identity=directory_identity,
            tree_sha256=identity[2],
        )
        budget = MODULE._PendingRegularSourceEvidenceBudget()

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_regular_source_payload",
                wraps=MODULE._read_pending_regular_source_payload,
            ) as read_source,
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
            first = budget.evidence(home, record, expectation)
            self.assertEqual(capture_release.call_count, 1)
            self.assertEqual(revalidate_release.call_count, 0)
            release_root = MODULE._releases_root(home, MODULE.PUBLIC_OWNER) / SHA_A
            manifest = release_root / MODULE.MANIFEST_RELATIVE_PATH
            replacement = manifest.with_name("replacement-manifest.json")
            replacement.write_bytes(manifest.read_bytes() + b"\n")
            replacement.chmod(manifest.stat().st_mode & 0o777)
            os.replace(replacement, manifest)

            cached = budget.evidence(home, record, expectation)
            self.assertIs(cached, first)
            self.assertEqual(revalidate_release.call_count, 0)
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release source .*pending regular-file source evidence cache "
                "revalidation",
            ):
                budget.finalize(home)

        self.assertEqual(read_source.call_count, 1)
        self.assertEqual(capture_release.call_count, 1)
        self.assertEqual(revalidate_release.call_count, 1)
        self.assertEqual(budget.evidence_read_bytes, first.size)
        self.assertTrue(budget.sealed)

    def test_pending_regular_source_budget_read_stays_on_preflight_fd(self) -> None:
        home = self.root / "home-v10-source-budget-binding"
        release = self.root / "release-v10-source-budget-binding"
        source = PurePosixPath("personal_codex/agents/reviewer.toml")
        original_payload = 'name = "a"\n'
        replacement_payload = 'name = "replacement-too-large"\n'
        write_agent_source_batch(
            release,
            {ROLE_TARGET: (source, original_payload)},
        )
        install(release, home, SHA_A)
        state = MODULE._load_managed_state(home)
        record = state.links[ROLE_TARGET]
        identity, directory_identity = (
            MODULE._installed_release_identity_and_directory_identity(
                home,
                MODULE.PUBLIC_OWNER,
                SHA_A,
            )
        )
        expectation = MODULE.PendingReleaseExpectation(
            owner=MODULE.PUBLIC_OWNER,
            sha=SHA_A,
            directory_identity=directory_identity,
            tree_sha256=identity[2],
        )
        budget = MODULE._PendingRegularSourceEvidenceBudget()
        installed_source = MODULE._record_regular_source_path(home, record)
        real_open = MODULE._open_bounded_regular_file

        def replace_after_open(*args: object, **kwargs: object):
            opened = real_open(*args, **kwargs)
            replacement = installed_source.with_name("replacement.toml")
            replacement.write_text(replacement_payload, encoding="utf-8")
            replacement.chmod(installed_source.stat().st_mode & 0o777)
            os.replace(replacement, installed_source)
            return opened

        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_EVIDENCE_READ_BYTES",
                len(original_payload.encode("utf-8")),
            ),
            mock.patch.object(
                MODULE,
                "_open_bounded_regular_file",
                side_effect=replace_after_open,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_source_payload",
                side_effect=AssertionError("source path must not be reopened"),
            ) as reopened_source,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "source evidence changed while reading",
            ),
        ):
            budget.evidence(home, record, expectation)

        reopened_source.assert_not_called()
        self.assertEqual(budget.evidence_read_bytes, 0)

    def test_v10_parser_budgets_distinct_sources_before_second_read(self) -> None:
        home = self.root / "home-v10-distinct-source-budget"
        first = self.root / "release-v10-distinct-source-budget-a"
        second = self.root / "release-v10-distinct-source-budget-b"
        reviewer_source = PurePosixPath("personal_codex/agents/reviewer.toml")
        worker_source = PurePosixPath("personal_codex/agents/worker.toml")
        reviewer_payload = 'name = "reviewer-budget"\n'
        worker_payload = 'name = "worker-budget"\n'
        sources_by_target = {
            ROLE_TARGET: (reviewer_source, reviewer_payload),
            UNCHANGED_ROLE_TARGET: (worker_source, worker_payload),
        }
        write_agent_source_batch(first, sources_by_target)
        write_agent_to_symlink_batch(
            second,
            {
                target: source
                for target, (source, _payload) in sources_by_target.items()
            },
        )
        install(first, home, SHA_A)
        self._retain_batch(
            home,
            second,
            committed=True,
            legacy_symlink=False,
            sha=SHA_B,
        )

        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_EVIDENCE_READ_BYTES",
                len(reviewer_payload.encode("utf-8")),
            ),
            mock.patch.object(
                MODULE,
                "_read_pending_regular_source_payload",
                wraps=MODULE._read_pending_regular_source_payload,
            ) as read_source,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "source evidence reads exceed the aggregate size limit",
            ),
        ):
            MODULE._load_pending_link_batch(home)

        self.assertEqual(read_source.call_count, 1)

    def test_final_regular_verification_preflights_three_pass_group(self) -> None:
        home, state, group_size = self._terminal_regular_budget_fixture("final")
        expectations = MODULE._stage_pending_terminal_regular_targets(
            home,
            state,
            (),
            phase="after",
        )
        ticket = MODULE.PendingBatchCleanupTicket(
            version=4,
            phase="after",
            path=self.root / "terminal-budget-ticket.json",
            snapshot=MODULE.ManagedStateFileSnapshot(
                exists=True,
                payload=b"ticket\n",
                mode=0o600,
                parent_identity=(0, 1),
                file_identity=(1, 2),
                file_type=stat.S_IFREG,
                size=7,
                uid=os.geteuid(),
                gid=os.getegid(),
            ),
            batch_root=self.root / "terminal-budget-batch",
            batch_root_identity=(3, 4),
            marker_path=PurePosixPath("pending/state/commit.json"),
            marker_parent_identity=(5, 6),
            marker_file_identity=(7, 8),
            marker_mode=0o600,
            marker_sha256="f" * 64,
            terminal_regular_targets=expectations,
        )

        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                group_size - 1,
            ),
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=ticket,
            ) as read_ticket,
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                wraps=MODULE._read_regular_file_snapshot_beneath,
            ) as read_snapshot,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "terminal regular.*aggregate size limit",
            ),
        ):
            MODULE._verify_final_regular_targets(home, ticket)
        self.assertEqual(read_ticket.call_count, 0)
        self.assertEqual(read_snapshot.call_count, 0)

        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                group_size,
            ),
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=ticket,
            ) as read_ticket,
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                wraps=MODULE._read_regular_file_snapshot_beneath,
            ) as read_snapshot,
        ):
            MODULE._verify_final_regular_targets(home, ticket)
        self.assertEqual(read_ticket.call_count, 4)
        self.assertEqual(read_snapshot.call_count, 3 * len(expectations))

    def test_v6_active_pointer_recovers_without_legacy_staging_authority(
        self,
    ) -> None:
        home = self.root / "home-v6-no-staging-authority"
        release = self.root / "release-v6-no-staging-authority"
        write_agent_release(release)
        batch = self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._downgrade_metadata(batch, 6)
        self._drop_cleanup_index(home)

        parsed = MODULE._load_pending_link_batch(home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        state, snapshot = MODULE._load_managed_state_with_snapshot(home)
        with contextlib.redirect_stdout(io.StringIO()):
            _state, _snapshot, recovered = MODULE._recover_pending_link_transaction(
                home,
                state,
                snapshot,
                dry_run=False,
            )

        self.assertTrue(recovered)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_v6_missing_cleanup_index_rejects_partial_staging_authority(
        self,
    ) -> None:
        for residue in ("marker", "temp", "retained"):
            with self.subTest(residue=residue):
                home = self.root / f"home-v6-partial-{residue}"
                release = self.root / f"release-v6-partial-{residue}"
                write_agent_release(release)
                batch = self._retain_batch(
                    home,
                    release,
                    committed=True,
                    legacy_symlink=False,
                )
                self._downgrade_metadata(batch, 6)
                self._drop_cleanup_index(home)
                parsed = MODULE._load_pending_link_batch(home)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                marker = batch.batch_root / Path(
                    *MODULE.PENDING_STATE_STAGING_MARKER.parts
                )
                if residue == "marker":
                    residue_path = marker
                elif residue == "temp":
                    residue_path = marker.with_name(
                        marker.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
                    )
                else:
                    residue_path = marker.with_name(
                        f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{marker.name}-"
                        "123-0123456789abcdef"
                    )
                residue_path.write_bytes(b"partial\n")
                residue_path.chmod(0o600)

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "partial legacy staging cleanup authority",
                ):
                    MODULE._read_or_restore_active_pointer_cleanup_ticket(
                        home,
                        parsed,
                    )

    def test_v8_active_pointer_rejects_missing_cleanup_index(self) -> None:
        home = self.root / "home-v8-no-cleanup-index"
        release = self.root / "release-v8-no-cleanup-index"
        write_agent_release(release)
        batch = self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._downgrade_metadata(batch, 8)
        self._drop_cleanup_index(home)
        parsed = MODULE._load_pending_link_batch(home)
        self.assertIsNotNone(parsed)
        assert parsed is not None

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "cleanup index is missing: metadata v8",
        ):
            MODULE._read_or_restore_active_pointer_cleanup_ticket(home, parsed)

    def test_pending_backup_keeps_unreadable_distinct_from_absent(self) -> None:
        home = self.root / "home-backup-tristate"
        first = self.root / "release-backup-tristate-a"
        second = self.root / "release-backup-tristate-b"
        write_agent_release(first, payload='name = "reviewer-a"\n')
        write_agent_release(second, payload='name = "reviewer-b"\n')
        install(first, home, SHA_A)
        batch = self._retain_batch(
            home,
            second,
            committed=True,
            legacy_symlink=False,
            sha=SHA_B,
        )
        record = next(record for record in batch.records if record.backup is not None)
        assert record.backup is not None
        backup = batch.batch_root / Path(*record.backup.parts)
        real_stat = os.stat

        def deny_backup(name, *args, dir_fd=None, **kwargs):
            if dir_fd is not None and name == backup.name:
                raise PermissionError("injected unreadable backup")
            return real_stat(name, *args, dir_fd=dir_fd, **kwargs)

        with (
            mock.patch.object(MODULE.os, "stat", side_effect=deny_backup),
            self.assertRaisesRegex(MODULE.SyncError, "pending backup is unreadable"),
        ):
            MODULE._pending_record_backup_snapshot(home, batch, record)

        self.assertTrue(os.path.lexists(backup))
        self.assertTrue(os.path.lexists(MODULE._pending_link_pointer_path(home)))

        backup.rename(backup.with_name(backup.name + ".missing"))
        self.assertIsNone(MODULE._pending_record_backup_snapshot(home, batch, record))

    def test_private_regular_files_override_a_fully_restrictive_umask(self) -> None:
        home = self.root / "home-umask-0777"
        target_parent = home / "agents"
        target_parent.mkdir(parents=True, mode=0o700)
        source = home / "source.toml"
        source.write_text('name = "reviewer"\n', encoding="utf-8")
        target = target_parent / "reviewer.toml"
        internal = home / "authority.json"
        real_mkdir = os.mkdir

        def create_safe_directory(name, mode=0o777, *, dir_fd=None):
            real_mkdir(name, mode, dir_fd=dir_fd)
            os.chmod(name, mode, dir_fd=dir_fd)

        prior_umask = os.umask(0o777)
        try:
            MODULE._create_regular_file_beneath(home, source, target)
            MODULE._write_exclusive_internal_file(home, internal, b"authority\n")
            with mock.patch.object(
                MODULE.os,
                "mkdir",
                side_effect=create_safe_directory,
            ):
                batch_root = MODULE._quarantine_batch_root(home, [])
        finally:
            os.umask(prior_umask)

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(internal.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((batch_root / "metadata.json").stat().st_mode),
            0o600,
        )

    def test_full_install_still_succeeds_under_the_normal_umask(self) -> None:
        home = self.root / "home-normal-umask"
        release = self.root / "release-normal-umask"
        write_agent_release(release)

        install(release, home, SHA_A)

        target = home / ROLE_TARGET
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_internal_file_fchmod_failure_clears_the_final_name(self) -> None:
        home = self.root / "home-internal-fchmod-failure"
        home.mkdir()
        path = home / "authority.json"

        with (
            mock.patch.object(
                MODULE.os,
                "fchmod",
                side_effect=OSError("injected fchmod failure"),
            ),
            self.assertRaisesRegex(OSError, "injected fchmod failure"),
        ):
            MODULE._write_exclusive_internal_file(home, path, b"authority\n")

        self.assertFalse(os.path.lexists(path))

    def test_internal_file_fchmod_and_cleanup_failure_remains_fail_closed(self) -> None:
        home = self.root / "home-internal-fchmod-and-cleanup-failure"
        home.mkdir()
        path = home / "authority.json"
        creation_error = "injected fchmod failure"
        cleanup_error = "injected cleanup failure"

        with (
            mock.patch.object(MODULE.os, "fchmod", side_effect=OSError(creation_error)),
            mock.patch.object(
                MODULE,
                "_cleanup_created_exclusive_internal_file",
                side_effect=MODULE.SyncError(cleanup_error),
            ),
        ):
            if callable(getattr(OSError(), "add_note", None)):
                with self.assertRaisesRegex(OSError, creation_error) as raised:
                    MODULE._write_exclusive_internal_file(home, path, b"authority\n")
                notes = "\n".join(getattr(raised.exception, "__notes__", ()))
                self.assertIn("final name could not be safely cleared", notes)
                self.assertIn(cleanup_error, notes)
            else:
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "final name could not be safely cleared",
                ) as raised:
                    MODULE._write_exclusive_internal_file(home, path, b"authority\n")
                self.assertIn(cleanup_error, str(raised.exception))
                self.assertIn(creation_error, str(raised.exception))
                self.assertIsInstance(raised.exception.__cause__, MODULE.SyncError)

        self.assertTrue(os.path.lexists(path))


if __name__ == "__main__":
    unittest.main()
