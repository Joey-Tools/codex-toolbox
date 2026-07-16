from __future__ import annotations

import contextlib
from collections.abc import Iterator
import functools
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_reconciliation_safety",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA_A = "a" * 40
SHA_B = "b" * 40


def write_skill_release(
    release_root: Path,
    *,
    source_name: str = "example",
    target_name: str = "example",
    owner: str = MODULE.PUBLIC_OWNER,
    base_release_sha: str | None = None,
) -> MODULE.ManifestData:
    skill_root = release_root / "personal_codex" / "skills" / source_name
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    manifest_path = release_root / MODULE.MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": [
            {
                "source": f"personal_codex/skills/{source_name}",
                "target": f"skills/{target_name}",
                "kind": "skill",
                "owner": owner,
            }
        ],
    }
    if base_release_sha is not None:
        payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_release_sha,
        }
    manifest_path.write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )
    return MODULE.load_manifest_data(release_root)


def install_quietly(source_root: Path, home: Path, sha: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(source_root, home, sha, dry_run=False)


def acquire_install_lock(home: Path) -> None:
    with MODULE.installation_lock(home):
        pass


def planned_reconcile_action(
    home: Path,
    action: str,
    target: Path,
    link_target: str,
    kind: str,
    **kwargs: object,
) -> MODULE.ReconcileAction:
    return MODULE.ReconcileAction(
        action,
        target,
        link_target,
        kind,
        planned_snapshot=MODULE._capture_reconcile_target_snapshot(home, target),
        **kwargs,
    )


class InstallLockBindingSafetyTests(unittest.TestCase):
    def _assert_replacement_cannot_bypass_stable_lock(
        self,
        replacement: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            home.mkdir()
            sync_root = home / "personal-sync"
            entered = threading.Event()
            started = threading.Event()
            thread_errors: list[BaseException] = []

            def acquire_replacement_lock() -> None:
                started.set()
                try:
                    with MODULE.installation_lock(home):
                        entered.set()
                except BaseException as error:
                    thread_errors.append(error)

            worker = threading.Thread(target=acquire_replacement_lock, daemon=True)
            blocked_while_first_held = False
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "binding changed during transaction",
            ):
                with MODULE.installation_lock(home):
                    lock_path = sync_root / "install.lock"
                    if replacement == "lock":
                        lock_path.rename(sync_root / "install.lock.displaced")
                        lock_path.write_text("replacement\n", encoding="utf-8")
                    else:
                        sync_root.rename(home / "personal-sync-displaced")
                        sync_root.mkdir(mode=0o700)
                        lock_path.write_text("replacement\n", encoding="utf-8")
                    worker.start()
                    self.assertTrue(started.wait(1.0))
                    blocked_while_first_held = not entered.wait(0.25)

            worker.join(3.0)
            self.assertFalse(worker.is_alive())
            self.assertTrue(blocked_while_first_held)
            self.assertTrue(entered.is_set())
            self.assertEqual(thread_errors, [])

    def test_lock_replacement_cannot_bypass_stable_home_lock(self) -> None:
        self._assert_replacement_cannot_bypass_stable_lock("lock")

    def test_sync_root_replacement_cannot_bypass_stable_home_lock(self) -> None:
        self._assert_replacement_cannot_bypass_stable_lock("sync-root")

    def test_parent_replacement_after_flock_does_not_enter_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            home.mkdir()
            sync_root = home / "personal-sync"
            displaced_root = home / "personal-sync-displaced"
            real_flock = MODULE.fcntl.flock
            replaced = False

            def replace_parent_after_flock(file_descriptor: int, operation: int) -> None:
                nonlocal replaced
                real_flock(file_descriptor, operation)
                if (
                    operation == MODULE.fcntl.LOCK_EX
                    and sync_root.exists()
                    and not replaced
                ):
                    replaced = True
                    sync_root.rename(displaced_root)
                    sync_root.mkdir(mode=0o700)
                    (sync_root / "install.lock").write_text(
                        "racer\n",
                        encoding="utf-8",
                    )

            transaction_body = mock.Mock()
            with (
                mock.patch.object(
                    MODULE.fcntl,
                    "flock",
                    side_effect=replace_parent_after_flock,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "install lock binding changed after acquisition",
                ),
            ):
                with MODULE.installation_lock(home):
                    transaction_body()

            transaction_body.assert_not_called()
            self.assertTrue((displaced_root / "install.lock").is_file())
            self.assertEqual(
                (sync_root / "install.lock").read_text(encoding="utf-8"),
                "racer\n",
            )

    def test_lock_replacement_after_flock_does_not_enter_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            home.mkdir()
            lock_path = home / "personal-sync" / "install.lock"
            displaced_lock = home / "personal-sync" / "install.lock.displaced"
            real_flock = MODULE.fcntl.flock
            replaced = False

            def replace_lock_after_flock(file_descriptor: int, operation: int) -> None:
                nonlocal replaced
                real_flock(file_descriptor, operation)
                if (
                    operation == MODULE.fcntl.LOCK_EX
                    and lock_path.exists()
                    and not replaced
                ):
                    replaced = True
                    lock_path.rename(displaced_lock)
                    lock_path.write_text("racer\n", encoding="utf-8")

            transaction_body = mock.Mock()
            with (
                mock.patch.object(
                    MODULE.fcntl,
                    "flock",
                    side_effect=replace_lock_after_flock,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "install lock binding changed after acquisition",
                ),
            ):
                with MODULE.installation_lock(home):
                    transaction_body()

            transaction_body.assert_not_called()
            self.assertTrue(displaced_lock.is_file())
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "racer\n")

    def test_home_replacement_after_flock_does_not_enter_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            home.mkdir()
            displaced_home = root / "home-displaced"
            real_flock = MODULE.fcntl.flock
            replaced = False

            def replace_home_after_flock(
                file_descriptor: int,
                operation: int,
            ) -> None:
                nonlocal replaced
                real_flock(file_descriptor, operation)
                if operation == MODULE.fcntl.LOCK_EX and not replaced:
                    replaced = True
                    home.rename(displaced_home)
                    home.symlink_to(displaced_home, target_is_directory=True)

            transaction_body = mock.Mock()
            with (
                mock.patch.object(
                    MODULE.fcntl,
                    "flock",
                    side_effect=replace_home_after_flock,
                ),
                self.assertRaisesRegex(MODULE.SyncError, "stable home changed"),
            ):
                with MODULE.installation_lock(home):
                    transaction_body()

            transaction_body.assert_not_called()
            self.assertTrue(home.is_symlink())
            self.assertFalse((displaced_home / "personal-sync").exists())


class ManifestSchemaSafetyTests(unittest.TestCase):
    def test_runtime_rejects_explicit_null_manifest_and_link_owners(self) -> None:
        for field in ("manifest", "link"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                release_root = Path(temp_dir) / "release"
                write_skill_release(release_root)
                manifest_path = release_root / MODULE.MANIFEST_RELATIVE_PATH
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                if field == "manifest":
                    payload["owner"] = None
                else:
                    payload["links"][0]["owner"] = None
                manifest_path.write_text(
                    json.dumps(payload) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(MODULE.SyncError, "owner id"):
                    MODULE.load_manifest_data(release_root)

    def test_runtime_rejects_unknown_removed_link_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            release_root = Path(temp_dir) / "release"
            write_skill_release(release_root)
            manifest_path = release_root / MODULE.MANIFEST_RELATIVE_PATH
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["removed_links"] = [
                {
                    "id": "retired",
                    "source": "personal_codex/skills/retired",
                    "target": "skills/retired",
                    "kind": "skill",
                    "legacy": True,
                    "future_note": "must not be ignored",
                }
            ]
            manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(MODULE.SyncError, "unsupported field"):
                MODULE.load_manifest_data(release_root)

    def test_owner_validation_rejects_manifest_changed_after_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            release_root = Path(temp_dir) / "release"
            manifest = write_skill_release(release_root, owner="private")
            expectation = MODULE._source_release_identity(release_root, manifest)
            manifest_path = release_root / MODULE.MANIFEST_RELATIVE_PATH
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["base_release"] = {"repo": "attacker/unexpected"}
            manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release manifest changed after install preflight",
            ):
                MODULE._validate_release_manifest_owner(
                    release_root,
                    "private",
                    expectation,
                )


class TargetPortabilityTests(unittest.TestCase):
    def test_rejects_case_variant_of_sync_internal_target(self) -> None:
        with self.assertRaisesRegex(MODULE.SyncError, "sync internal path"):
            MODULE._validate_target_path(
                "Personal-Sync/state/managed-links.json",
                "target",
            )

    def test_rejects_pending_pointer_portable_variants_and_descendants(self) -> None:
        for target in (
            MODULE.PENDING_LINK_POINTER_NAME,
            ".Personal-Sync-Pending-Transaction.JSON",
            ".per\N{LATIN SMALL LETTER LONG S}onal-sync-pending-transaction.json",
            f"{MODULE.PENDING_LINK_POINTER_NAME}/child",
        ):
            with self.subTest(target=target), self.assertRaisesRegex(
                MODULE.SyncError,
                "pending transaction pointer path",
            ):
                MODULE._validate_target_path(target, "target")

        self.assertEqual(
            MODULE._validate_target_path(
                f"skills/{MODULE.PENDING_LINK_POINTER_NAME}",
                "target",
            ),
            PurePosixPath("skills") / MODULE.PENDING_LINK_POINTER_NAME,
        )

    def test_rejects_different_spellings_with_same_portable_key(self) -> None:
        composed = PurePosixPath("skills/caf\N{LATIN SMALL LETTER E WITH ACUTE}")
        decomposed = PurePosixPath("skills/cafe\N{COMBINING ACUTE ACCENT}")
        active = MODULE.LinkEntry(
            source=PurePosixPath("skills/example"),
            target=composed,
            kind="skill",
        )

        cases = (
            MODULE.RemovedLink(
                id="old-skill",
                source=PurePosixPath("skills/old"),
                target=decomposed,
                kind="skill",
                owner=MODULE.PUBLIC_OWNER,
            ),
            MODULE.RemovedLink(
                id="replacement",
                source=PurePosixPath("skills/old"),
                target=PurePosixPath("skills/old"),
                kind="skill",
                owner=MODULE.PUBLIC_OWNER,
                replacement_target=decomposed,
            ),
        )
        for removed in cases:
            with self.subTest(removed=removed.id):
                manifest = MODULE.ManifestData(
                    owner=MODULE.PUBLIC_OWNER,
                    entries=[active],
                    removed_links=[removed],
                )
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "portable target spellings conflict",
                ):
                    MODULE._validate_manifest_target_portability([manifest])

    def test_allows_exact_active_and_removed_tombstone_target(self) -> None:
        target = PurePosixPath("skills/example")
        manifest = MODULE.ManifestData(
            owner=MODULE.PUBLIC_OWNER,
            entries=[
                MODULE.LinkEntry(
                    source=PurePosixPath("skills/example"),
                    target=target,
                    kind="skill",
                )
            ],
            removed_links=[
                MODULE.RemovedLink(
                    id="example",
                    source=PurePosixPath("skills/example"),
                    target=target,
                    kind="skill",
                    owner=MODULE.PUBLIC_OWNER,
                )
            ],
        )

        MODULE._validate_manifest_target_portability([manifest])

    @staticmethod
    def _target_manifest(
        owner: str,
        *,
        active: str | None = None,
        removed: str | None = None,
        replacement: str | None = None,
    ) -> MODULE.ManifestData:
        entries = (
            []
            if active is None
            else [
                MODULE.LinkEntry(
                    source=PurePosixPath("skills/active"),
                    target=PurePosixPath(active),
                    kind="skill",
                    owner=owner,
                )
            ]
        )
        removed_links = (
            []
            if removed is None
            else [
                MODULE.RemovedLink(
                    id="retired",
                    source=PurePosixPath("skills/retired"),
                    target=PurePosixPath(removed),
                    kind="skill",
                    owner=owner,
                    replacement_target=(
                        None if replacement is None else PurePosixPath(replacement)
                    ),
                )
            ]
        )
        return MODULE.ManifestData(
            owner=owner,
            entries=entries,
            removed_links=removed_links,
        )

    def test_rejects_cross_owner_active_removed_strict_ancestors_in_any_order(
        self,
    ) -> None:
        hierarchy_cases = (
            (
                "active ancestor",
                "skills/example",
                "skills/example/child",
            ),
            (
                "removed ancestor",
                "skills/example/child",
                "skills/example",
            ),
            (
                "portable active ancestor",
                "Skills/Cafe\N{COMBINING ACUTE ACCENT}",
                "skills/caf\N{LATIN SMALL LETTER E WITH ACUTE}/child",
            ),
            (
                "portable removed ancestor",
                "skills/caf\N{LATIN SMALL LETTER E WITH ACUTE}/child",
                "Skills/Cafe\N{COMBINING ACUTE ACCENT}",
            ),
        )
        owner_cases = (
            (MODULE.PUBLIC_OWNER, "private"),
            ("private", MODULE.PUBLIC_OWNER),
        )
        for case, active_target, removed_target in hierarchy_cases:
            for active_owner, removed_owner in owner_cases:
                active_manifest = self._target_manifest(
                    active_owner,
                    active=active_target,
                )
                removed_manifest = self._target_manifest(
                    removed_owner,
                    removed=removed_target,
                )
                for manifests in (
                    [active_manifest, removed_manifest],
                    [removed_manifest, active_manifest],
                ):
                    with self.subTest(
                        case=case,
                        active_owner=active_owner,
                        manifests=[manifest.owner for manifest in manifests],
                    ), self.assertRaisesRegex(
                        MODULE.SyncError,
                        "active and removed targets must not overlap across owners",
                    ):
                        MODULE._validate_manifest_target_portability(manifests)

    def test_allows_cross_owner_exact_active_removed_migration(self) -> None:
        target = "skills/example"
        for active_owner, removed_owner in (
            (MODULE.PUBLIC_OWNER, "private"),
            ("private", MODULE.PUBLIC_OWNER),
        ):
            active_manifest = self._target_manifest(active_owner, active=target)
            removed_manifest = self._target_manifest(removed_owner, removed=target)

            with self.subTest(active_owner=active_owner):
                MODULE._validate_manifest_target_portability(
                    [active_manifest, removed_manifest]
                )

    def test_ignores_removed_replacement_target_hierarchy(self) -> None:
        for active_target, replacement_target in (
            ("skills/example", "skills/example/child"),
            ("skills/example/child", "skills/example"),
        ):
            manifests = [
                self._target_manifest(
                    MODULE.PUBLIC_OWNER,
                    active=active_target,
                ),
                self._target_manifest(
                    "private",
                    removed="skills/retired",
                    replacement=replacement_target,
                ),
            ]

            with self.subTest(
                active_target=active_target,
                replacement_target=replacement_target,
            ):
                MODULE._validate_manifest_target_portability(manifests)

    def test_allows_cross_owner_removed_history_hierarchy(self) -> None:
        manifests = [
            self._target_manifest(owner, removed=target)
            for owner, target in (
                (MODULE.PUBLIC_OWNER, "skills/example"),
                ("private", "skills/example/child"),
            )
        ]

        MODULE._validate_manifest_target_portability(manifests)

    def test_allows_same_owner_active_removed_hierarchy_migration(self) -> None:
        for active_target, removed_target in (
            ("skills/example", "skills/example/child"),
            ("skills/example/child", "skills/example"),
        ):
            manifest = self._target_manifest(
                "private",
                active=active_target,
                removed=removed_target,
            )

            with self.subTest(
                active_target=active_target,
                removed_target=removed_target,
            ):
                MODULE._validate_manifest_target_portability([manifest])

    def test_rejects_portable_active_collision_and_ancestor(self) -> None:
        cases = (
            (PurePosixPath("skills/Foo"), PurePosixPath("skills/foo")),
            (PurePosixPath("Skills/Foo"), PurePosixPath("skills/foo/bar")),
        )
        for first, second in cases:
            with self.subTest(first=first, second=second):
                with self.assertRaises(MODULE.SyncError):
                    MODULE._validate_non_overlapping_targets([first, second])

    def test_non_overlapping_validation_normalizes_each_target_constant_times(
        self,
    ) -> None:
        targets = [
            PurePosixPath(f"skills/item-{index:05d}")
            for index in range(2_048)
        ]
        real_portable_target_key = MODULE._portable_target_key

        with mock.patch.object(
            MODULE,
            "_portable_target_key",
            wraps=real_portable_target_key,
        ) as portable_target_key:
            MODULE._validate_non_overlapping_targets(targets)

        self.assertLessEqual(portable_target_key.call_count, 2 * len(targets))

    def test_install_rejects_cross_version_portable_conflicts_before_staging(
        self,
    ) -> None:
        composed = "caf\N{LATIN SMALL LETTER E WITH ACUTE}"
        decomposed = "cafe\N{COMBINING ACUTE ACCENT}"
        cases = (
            (
                "case-only identity",
                PurePosixPath("skills/Widget"),
                PurePosixPath("skills/widget"),
                "portable target spellings conflict",
            ),
            (
                "unicode identity",
                PurePosixPath(f"skills/{composed}"),
                PurePosixPath(f"skills/{decomposed}"),
                "portable target spellings conflict",
            ),
            (
                "portable ancestor",
                PurePosixPath(f"Skills/{composed}"),
                PurePosixPath(f"skills/{decomposed}/child"),
                "manifest targets must not overlap",
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, current_target, next_target, message in cases:
                with self.subTest(case=name):
                    current_manifest = MODULE.ManifestData(
                        owner=MODULE.PUBLIC_OWNER,
                        entries=[
                            MODULE.LinkEntry(
                                source=PurePosixPath("personal_codex/skills/current"),
                                target=current_target,
                                kind="skill",
                            )
                        ],
                        removed_links=[],
                    )
                    next_manifest = MODULE.ManifestData(
                        owner=MODULE.PUBLIC_OWNER,
                        entries=[
                            MODULE.LinkEntry(
                                source=PurePosixPath("personal_codex/skills/next"),
                                target=next_target,
                                kind="skill",
                            )
                        ],
                        removed_links=[],
                    )
                    with (
                        mock.patch.object(
                            MODULE,
                            "_installed_manifests",
                            return_value={MODULE.PUBLIC_OWNER: current_manifest},
                        ),
                        mock.patch.object(
                            MODULE,
                            "_stage_release_tree_for_install",
                        ) as stage_release,
                    ):
                        with self.assertRaisesRegex(MODULE.SyncError, message):
                            MODULE._install_release_set_unlocked(
                                root / name,
                                [(root / "release", SHA_B, next_manifest)],
                                dry_run=False,
                                allow_cross_owner=False,
                            )
                    stage_release.assert_not_called()


class ArchiveErrorNormalizationSafetyTests(unittest.TestCase):
    def test_truncated_gzip_footer_is_reported_as_sync_error(self) -> None:
        archive_payload = io.BytesIO()
        with MODULE.tarfile.open(fileobj=archive_payload, mode="w:gz") as archive:
            directory = MODULE.tarfile.TarInfo("personal-codex-test/")
            directory.type = MODULE.tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)

        truncated = archive_payload.getvalue()[:-8]
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "failed to inspect archive snapshot",
        ):
            MODULE._scan_archive_snapshot(io.BytesIO(truncated))


class PendingTransactionCapacitySafetyTests(unittest.TestCase):
    def test_same_release_reserves_current_action_before_release_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            release = root / "release"
            manifest = write_skill_release(release)
            install_quietly(release, home, SHA_A)

            with (
                mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 0),
                mock.patch.object(
                    MODULE,
                    "_stage_release_tree_for_install",
                    side_effect=AssertionError(
                        "release staging must follow worst-case capacity validation"
                    ),
                ) as stage_release,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending transaction has too many actions",
                ),
            ):
                MODULE._install_release_set_unlocked(
                    home,
                    [(release, SHA_A, manifest)],
                    dry_run=False,
                    allow_cross_owner=False,
                )

            stage_release.assert_not_called()
            self.assertEqual(MODULE._current_sha(home), SHA_A)
            self.assertTrue((home / "skills" / "example").is_symlink())
            self.assertFalse(
                os.path.lexists(home / MODULE.PENDING_LINK_POINTER_NAME)
            )

    def test_upgrade_rejects_record_overflow_before_release_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            current_release = root / "current-release"
            next_release = root / "next-release"
            write_skill_release(
                current_release,
                source_name="current",
                target_name="current",
            )
            write_skill_release(
                next_release,
                source_name="next",
                target_name="next",
            )
            install_quietly(current_release, home, SHA_A)

            with (
                mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 2),
                mock.patch.object(
                    MODULE,
                    "_stage_release_tree_for_install",
                    side_effect=AssertionError(
                        "release staging must follow transaction capacity validation"
                    ),
                ) as stage_release,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending transaction has too many actions",
                ),
            ):
                MODULE.install_release_tree(
                    next_release,
                    home,
                    SHA_B,
                    dry_run=False,
                )

            stage_release.assert_not_called()
            self.assertEqual(MODULE._current_sha(home), SHA_A)
            self.assertTrue((home / "skills" / "current").is_symlink())
            self.assertFalse(os.path.lexists(home / "skills" / "next"))
            self.assertFalse((MODULE._releases_root(home) / SHA_B).exists())
            self.assertFalse(
                os.path.lexists(home / MODULE.PENDING_LINK_POINTER_NAME)
            )

    def test_first_install_rejects_claim_overflow_before_release_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            release = root / "release"
            write_skill_release(release)

            with (
                mock.patch.object(MODULE, "MAX_PENDING_LINK_CLAIMS", 1),
                mock.patch.object(
                    MODULE,
                    "_stage_release_tree_for_install",
                    side_effect=AssertionError(
                        "release staging must follow transaction capacity validation"
                    ),
                ) as stage_release,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending transaction has too many after-state claims",
                ),
            ):
                MODULE.install_release_tree(
                    release,
                    home,
                    SHA_A,
                    dry_run=False,
                )

            stage_release.assert_not_called()
            self.assertIsNone(MODULE._current_sha(home))
            self.assertFalse((MODULE._releases_root(home) / SHA_A).exists())
            self.assertFalse(
                os.path.lexists(home / MODULE.PENDING_LINK_POINTER_NAME)
            )

    def test_first_install_rejects_state_overflow_before_release_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            release = root / "release"
            write_skill_release(release)

            with (
                mock.patch.object(MODULE, "MAX_MANAGED_STATE_BYTES", 1),
                mock.patch.object(
                    MODULE,
                    "_stage_release_tree_for_install",
                    side_effect=AssertionError(
                        "release staging must follow transaction capacity validation"
                    ),
                ) as stage_release,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "planned managed state exceeds the size limit",
                ),
            ):
                MODULE.install_release_tree(
                    release,
                    home,
                    SHA_A,
                    dry_run=False,
                )

            stage_release.assert_not_called()
            self.assertIsNone(MODULE._current_sha(home))
            self.assertFalse((MODULE._releases_root(home) / SHA_A).exists())
            self.assertFalse(
                os.path.lexists(home / MODULE.PENDING_LINK_POINTER_NAME)
            )

    def test_first_install_rejects_release_overflow_before_release_staging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            release = root / "release"
            write_skill_release(release)

            with (
                mock.patch.object(MODULE, "MAX_PENDING_RELEASES", 0),
                mock.patch.object(
                    MODULE,
                    "_stage_release_tree_for_install",
                    side_effect=AssertionError(
                        "release staging must follow transaction capacity validation"
                    ),
                ) as stage_release,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "too many after-state release expectations",
                ),
            ):
                MODULE.install_release_tree(
                    release,
                    home,
                    SHA_A,
                    dry_run=False,
                )

            stage_release.assert_not_called()
            self.assertIsNone(MODULE._current_sha(home))
            self.assertFalse((MODULE._releases_root(home) / SHA_A).exists())
            self.assertFalse(
                os.path.lexists(home / MODULE.PENDING_LINK_POINTER_NAME)
            )

    def test_first_install_rejects_metadata_overflow_before_release_staging(
        self,
    ) -> None:
        self.assertEqual(
            len(MODULE._MAX_PENDING_BATCH_NAME.encode("utf-8")),
            MODULE.MAX_PENDING_LINK_BATCH_NAME_BYTES,
        )
        self.assertIsNotNone(
            MODULE.PENDING_LINK_BATCH_RE.fullmatch(
                MODULE._MAX_PENDING_BATCH_NAME
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            release = root / "release"
            write_skill_release(release)

            with (
                mock.patch.object(MODULE, "MAX_MANAGED_STATE_BYTES", 4096),
                mock.patch.object(
                    MODULE,
                    "_stage_release_tree_for_install",
                    side_effect=AssertionError(
                        "release staging must follow metadata capacity validation"
                    ),
                ) as stage_release,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending link transaction metadata exceeds the size limit",
                ),
            ):
                MODULE.install_release_tree(
                    release,
                    home,
                    SHA_A,
                    dry_run=False,
                )

            stage_release.assert_not_called()
            self.assertIsNone(MODULE._current_sha(home))
            self.assertFalse((MODULE._releases_root(home) / SHA_A).exists())
            self.assertFalse(
                os.path.lexists(home / MODULE.PENDING_LINK_POINTER_NAME)
            )


class ReleaseShaBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.manifest = write_skill_release(self.source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_direct_install_rejects_invalid_sha_before_any_home_write(self) -> None:
        outside_absolute = self.root / "outside-absolute"
        invalid_shas = (
            "",
            ".",
            "..",
            "../../../outside-traversal",
            str(outside_absolute),
            SHA_A.upper(),
        )
        for index, sha in enumerate(invalid_shas):
            with self.subTest(sha=sha):
                home = self.root / f"home-direct-{index}"
                with self.assertRaisesRegex(MODULE.SyncError, "release SHA must"):
                    MODULE.install_release_tree(
                        self.source,
                        home,
                        sha,
                        dry_run=False,
                    )
                self.assertFalse(home.exists())
                self.assertFalse(outside_absolute.exists())
                self.assertFalse((self.root / "outside-traversal").exists())

    def test_release_set_rejects_invalid_sha_before_preflight(self) -> None:
        home = self.root / "home-release-set"
        with (
            mock.patch.object(MODULE, "_installed_manifests") as installed,
            mock.patch.object(MODULE, "_stage_release_tree_for_install") as stage,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "release SHA must"):
                MODULE._install_release_set_unlocked(
                    home,
                    [(self.source, "../../../outside-release-set", self.manifest)],
                    dry_run=False,
                    allow_cross_owner=False,
                )

        installed.assert_not_called()
        stage.assert_not_called()
        self.assertFalse(home.exists())
        self.assertFalse((self.root / "outside-release-set").exists())

    def test_staging_and_current_planning_validate_sha_at_their_boundaries(
        self,
    ) -> None:
        home = self.root / "home-boundaries"
        with mock.patch.object(MODULE, "_ensure_install_roots") as ensure_roots:
            with self.assertRaisesRegex(MODULE.SyncError, "release SHA must"):
                MODULE._stage_release_tree_for_install(
                    self.source,
                    home,
                    "../outside-stage",
                    self.manifest,
                )
        ensure_roots.assert_not_called()

        with mock.patch.object(
            MODULE,
            "_ensure_current_can_switch",
        ) as ensure_current:
            with self.assertRaisesRegex(MODULE.SyncError, "release SHA must"):
                MODULE._plan_current_switch_action(
                    home,
                    "../outside-current",
                )
        ensure_current.assert_not_called()
        self.assertFalse(home.exists())


class ReconciliationOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_failure_preserves_old_link_without_quarantine(self) -> None:
        old_target = self.home / "skills" / "old"
        old_target.parent.mkdir(parents=True)
        old_target.symlink_to("old-source")
        blocked_parent = self.home / "blocked"
        actions = [
            planned_reconcile_action(
                self.home,
                "quarantine-remove",
                old_target,
                "",
                "skill",
                expected_link_target="old-source",
                removed_link_key="public:old",
            ),
            planned_reconcile_action(
                self.home,
                "create",
                blocked_parent / "new",
                "new-source",
                "skill",
            ),
        ]
        blocked_parent.write_text("not a directory\n", encoding="utf-8")

        with self.assertRaises(MODULE.SyncError):
            MODULE._apply_reconcile_actions(self.home, actions, dry_run=False)

        self.assertTrue(old_target.is_symlink())
        self.assertEqual(os.readlink(old_target), "old-source")
        self.assertFalse((self.home / "personal-sync" / "quarantine").exists())

    def test_multiple_creates_share_transaction_created_parent_identity(self) -> None:
        first = self.home / "new-parent" / "first"
        second = self.home / "new-parent" / "second"
        actions = [
            planned_reconcile_action(self.home, "create", first, "first-source", "file"),
            planned_reconcile_action(
                self.home,
                "create",
                second,
                "second-source",
                "file",
            ),
        ]

        MODULE._apply_reconcile_actions(self.home, actions, dry_run=False)

        self.assertEqual(os.readlink(first), "first-source")
        self.assertEqual(os.readlink(second), "second-source")

    def test_create_rejects_external_parent_created_after_planning(self) -> None:
        target = self.home / "new-parent" / "example"
        action = planned_reconcile_action(
            self.home,
            "create",
            target,
            "example-source",
            "file",
        )
        target.parent.mkdir()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "parent appeared after planning",
        ):
            MODULE._apply_reconcile_actions(self.home, [action], dry_run=False)

        self.assertFalse(os.path.lexists(target))

    def test_verifies_create_before_removing_old_link(self) -> None:
        old_target = self.home / "skills" / "old"
        new_target = self.home / "skills" / "new"
        old_target.parent.mkdir(parents=True)
        old_target.symlink_to("old-source")
        actions = [
            planned_reconcile_action(
                self.home,
                "remove",
                old_target,
                "",
                "skill",
                expected_link_target="old-source",
            ),
            planned_reconcile_action(
                self.home,
                "create",
                new_target,
                "new-source",
                "skill",
            ),
        ]
        real_verify = MODULE._verify_reconcile_action_targets

        def verify_while_old_link_exists(
            home_to_verify: Path,
            actions_to_verify: list[object],
        ) -> None:
            self.assertEqual(home_to_verify, self.home)
            if actions_to_verify:
                self.assertTrue(old_target.is_symlink())
            real_verify(home_to_verify, actions_to_verify)

        with mock.patch.object(
            MODULE,
            "_verify_reconcile_action_targets",
            side_effect=verify_while_old_link_exists,
        ):
            MODULE._apply_reconcile_actions(self.home, actions, dry_run=False)

        self.assertFalse(old_target.is_symlink())
        self.assertTrue(new_target.is_symlink())
        self.assertEqual(os.readlink(new_target), "new-source")

    def test_orders_replacement_before_retiring_dependent_link(self) -> None:
        for replacement_action_name in ("replace", "quarantine-replace"):
            with self.subTest(replacement_action=replacement_action_name):
                home = self.home / replacement_action_name
                old_target = home / "skills" / "a-old"
                replacement_target = home / "skills" / "z-replacement"
                old_target.parent.mkdir(parents=True)
                removed = MODULE.RemovedLink(
                    id="move-old",
                    source=PurePosixPath("personal_codex/skills/retired-old"),
                    target=PurePosixPath("skills/a-old"),
                    kind="skill",
                    owner=MODULE.PUBLIC_OWNER,
                    replacement_target=PurePosixPath("skills/z-replacement"),
                )
                old_entry = MODULE.LinkEntry(
                    source=PurePosixPath("personal_codex/skills/refreshed-old"),
                    target=PurePosixPath("skills/a-old"),
                    kind="skill",
                )
                replacement_entry = MODULE.LinkEntry(
                    source=PurePosixPath("personal_codex/skills/replacement"),
                    target=PurePosixPath("skills/z-replacement"),
                    kind="skill",
                )
                old_link_target = MODULE._removed_link_target(home, removed)
                old_target.symlink_to(old_link_target)
                replacement_target.symlink_to("previous-replacement")
                actions = [
                    planned_reconcile_action(
                        home,
                        "replace",
                        old_target,
                        MODULE._desired_link_target(home, old_entry),
                        "skill",
                        expected_link_target=old_link_target,
                    ),
                    planned_reconcile_action(
                        home,
                        replacement_action_name,
                        replacement_target,
                        MODULE._desired_link_target(home, replacement_entry),
                        "skill",
                        expected_link_target="previous-replacement",
                        removed_link_key=(
                            "public:previous-replacement"
                            if replacement_action_name == "quarantine-replace"
                            else None
                        ),
                    ),
                ]
                required = MODULE._required_replacements_for_removals(
                    home,
                    actions,
                    [removed],
                    [old_entry, replacement_entry],
                )
                self.assertEqual(required, {old_target: [replacement_entry]})
                real_verify = MODULE._verify_reconcile_action_targets
                real_move = MODULE._atomic_move_beneath_home
                replacement_verified = False

                def track_verification(
                    home_to_verify: Path,
                    actions_to_verify: list[MODULE.ReconcileAction],
                ) -> None:
                    nonlocal replacement_verified
                    real_verify(home_to_verify, actions_to_verify)
                    if any(
                        action.target == replacement_target
                        for action in actions_to_verify
                    ):
                        replacement_verified = True

                def require_verification_before_old_retirement(
                    home_to_move: Path,
                    source: Path,
                    destination: Path,
                    expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
                    expected_destination_parent_identity: tuple[int, int] | None = None,
                ) -> None:
                    if source == old_target:
                        self.assertTrue(replacement_verified)
                    real_move(
                        home_to_move,
                        source,
                        destination,
                        expected_snapshot,
                        expected_destination_parent_identity,
                    )

                with mock.patch.object(
                    MODULE,
                    "_verify_reconcile_action_targets",
                    side_effect=track_verification,
                ), mock.patch.object(
                    MODULE,
                    "_atomic_move_beneath_home",
                    side_effect=require_verification_before_old_retirement,
                ):
                    MODULE._apply_reconcile_actions(
                        home,
                        actions,
                        dry_run=False,
                        required_replacements=required,
                    )

                self.assertEqual(
                    os.readlink(replacement_target),
                    MODULE._desired_link_target(home, replacement_entry),
                )
                self.assertEqual(
                    os.readlink(old_target),
                    MODULE._desired_link_target(home, old_entry),
                )

    def test_same_path_replacement_is_verified_after_creation(self) -> None:
        target = self.home / "skills" / "same-path"
        target.parent.mkdir(parents=True)
        removed = MODULE.RemovedLink(
            id="same-path-migration",
            source=PurePosixPath("personal_codex/skills/old"),
            target=PurePosixPath("skills/same-path"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            replacement_target=PurePosixPath("skills/same-path"),
        )
        replacement = MODULE.LinkEntry(
            source=PurePosixPath("personal_codex/skills/new"),
            target=PurePosixPath("skills/same-path"),
            kind="skill",
        )
        old_link_target = MODULE._removed_link_target(self.home, removed)
        target.symlink_to(old_link_target)
        action = planned_reconcile_action(
            self.home,
            "replace",
            target,
            MODULE._desired_link_target(self.home, replacement),
            "skill",
            expected_link_target=old_link_target,
        )
        required = MODULE._required_replacements_for_removals(
            self.home,
            [action],
            [removed],
            [replacement],
        )

        MODULE._apply_reconcile_actions(
            self.home,
            [action],
            dry_run=False,
            required_replacements=required,
        )

        self.assertEqual(
            os.readlink(target),
            MODULE._desired_link_target(self.home, replacement),
        )

    def test_dry_run_reports_phase_order_without_mutation(self) -> None:
        old_target = self.home / "old"
        new_target = self.home / "new"
        actions = [
            planned_reconcile_action(
                self.home,
                "remove",
                old_target,
                "",
                "file",
                expected_link_target="old-source",
            ),
            planned_reconcile_action(
                self.home,
                "create",
                new_target,
                "new-source",
                "file",
            ),
        ]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            MODULE._apply_reconcile_actions(self.home, actions, dry_run=True)

        lines = output.getvalue().splitlines()
        self.assertIn("would create", lines[0])
        self.assertIn("would remove", lines[1])
        self.assertFalse(old_target.exists())
        self.assertFalse(new_target.exists())
        self.assertFalse((self.home / "personal-sync" / "quarantine").exists())

    def test_restores_previous_current_pointer(self) -> None:
        sync_root = self.home / "personal-sync"
        sync_root.mkdir()
        current = sync_root / "current"
        current.symlink_to("releases/old")
        original_identity = (current.lstat().st_dev, current.lstat().st_ino)
        action = MODULE._plan_current_switch_action(
            self.home,
            "f" * 40,
        )
        self.assertIsNotNone(action)
        transaction = MODULE._apply_reconcile_actions(
            self.home,
            [action],
            dry_run=False,
        )
        MODULE._rollback_reconcile_transaction(self.home, transaction)

        self.assertTrue(current.is_symlink())
        self.assertEqual(os.readlink(current), "releases/old")
        self.assertEqual(
            (current.lstat().st_dev, current.lstat().st_ino),
            original_identity,
        )
        assert transaction is not None
        assert transaction.batch_root is not None
        self.assertFalse(
            os.path.lexists(
                transaction.batch_root / "links" / "personal-sync" / "current"
            )
        )

    def test_installed_manifests_reject_invalid_current_release(self) -> None:
        cases = (
            ("invalid-sha", "not-a-sha", "invalid release SHA"),
            ("missing-manifest", "a" * 40, "references invalid release"),
        )
        for case_name, current_target, message in cases:
            with self.subTest(case=case_name):
                home = self.home / case_name
                sync_root = home / "personal-sync"
                sync_root.mkdir(parents=True)
                releases_root = sync_root / "releases"
                releases_root.mkdir()
                if current_target != "not-a-sha":
                    (releases_root / current_target).mkdir()
                (sync_root / "current").symlink_to(
                    f"releases/{current_target}",
                    target_is_directory=True,
                )

                with self.assertRaisesRegex(MODULE.SyncError, message):
                    MODULE._installed_manifests(home)

    def test_rechecks_all_active_replacements_before_removal(self) -> None:
        for action_name in ("remove", "quarantine-remove"):
            with self.subTest(action=action_name):
                home = self.home / action_name
                old_target = home / "skills" / "old"
                replacement_target = home / "skills" / "new"
                old_target.parent.mkdir(parents=True)
                removed = MODULE.RemovedLink(
                    id="move-old-to-new",
                    source=PurePosixPath("personal_codex/skills/old"),
                    target=PurePosixPath("skills/old"),
                    kind="skill",
                    owner=MODULE.PUBLIC_OWNER,
                    replacement_target=PurePosixPath("skills/new"),
                )
                replacement = MODULE.LinkEntry(
                    source=PurePosixPath("personal_codex/skills/new"),
                    target=PurePosixPath("skills/new"),
                    kind="skill",
                )
                old_link_target = MODULE._removed_link_target(home, removed)
                old_target.symlink_to(old_link_target, target_is_directory=True)
                replacement_target.symlink_to(
                    MODULE._desired_link_target(home, replacement),
                    target_is_directory=True,
                )
                manifest = MODULE.ManifestData(
                    owner=MODULE.PUBLIC_OWNER,
                    entries=[replacement],
                    removed_links=[removed],
                )
                action = planned_reconcile_action(
                    home,
                    action_name,
                    old_target,
                    "",
                    "skill",
                    expected_link_target=old_link_target,
                    removed_link_key=(
                        "public:move-old-to-new"
                        if action_name == "quarantine-remove"
                        else None
                    ),
                )
                MODULE._validate_active_replacements(
                    home,
                    {},
                    {MODULE.PUBLIC_OWNER: manifest},
                    [replacement],
                )
                required_replacements = (
                    MODULE._required_replacements_for_removals(
                        home,
                        [action],
                        [removed],
                        [replacement],
                    )
                )
                self.assertEqual(required_replacements, {old_target: [replacement]})
                replacement_target.unlink()
                replacement_target.symlink_to(
                    "concurrent-drift",
                    target_is_directory=True,
                )

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "active replacement target changed before removal",
                ):
                    MODULE._apply_reconcile_actions(
                        home,
                        [action],
                        dry_run=False,
                        required_replacements=required_replacements,
                    )

                self.assertTrue(old_target.is_symlink())
                self.assertEqual(os.readlink(old_target), old_link_target)

    def test_replacement_requirement_ignores_stale_episode_link_target(self) -> None:
        target = self.home / "skills" / "old"
        stale_episode = MODULE.RemovedLink(
            id="old-v1",
            source=PurePosixPath("personal_codex/skills/old-v1"),
            target=PurePosixPath("skills/old"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            replacement_target=PurePosixPath("skills/missing"),
        )
        live_episode = MODULE.RemovedLink(
            id="old-v2",
            source=PurePosixPath("personal_codex/skills/old-v2"),
            target=PurePosixPath("skills/old"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            replacement_target=PurePosixPath("skills/new"),
        )
        replacement = MODULE.LinkEntry(
            source=PurePosixPath("personal_codex/skills/new"),
            target=PurePosixPath("skills/new"),
            kind="skill",
        )
        action = planned_reconcile_action(
            self.home,
            "quarantine-remove",
            target,
            "",
            "skill",
            expected_link_target=MODULE._removed_link_target(
                self.home,
                live_episode,
            ),
            removed_link_key="public:old-v2",
        )

        required = MODULE._required_replacements_for_removals(
            self.home,
            [action],
            [stale_episode, live_episode],
            [replacement],
        )

        self.assertEqual(required, {target: [replacement]})

    def test_cross_owner_replacement_requires_explicit_retirement(self) -> None:
        migrated = MODULE.RemovedLink(
            id="moved-to-public",
            source=PurePosixPath("personal_codex/skills/private-skill"),
            target=PurePosixPath("skills/private-skill"),
            kind="skill",
            owner="private",
            replacement_target=PurePosixPath("skills/public-skill"),
        )
        keep = MODULE.LinkEntry(
            source=PurePosixPath("personal_codex/skills/keep"),
            target=PurePosixPath("skills/keep"),
            kind="skill",
        )
        public_manifest = MODULE.ManifestData(
            owner=MODULE.PUBLIC_OWNER,
            entries=[keep],
            removed_links=[],
        )
        private_manifest = MODULE.ManifestData(
            owner="private",
            entries=[],
            removed_links=[migrated],
        )
        manifests = {
            MODULE.PUBLIC_OWNER: public_manifest,
            "private": private_manifest,
        }

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "replacement target skills/public-skill is unavailable",
        ):
            MODULE._validate_active_replacements(
                self.home,
                manifests,
                manifests,
                [keep],
            )

        retirement = MODULE.RemovedLink(
            id="remove-public-skill",
            source=PurePosixPath("personal_codex/skills/public-skill"),
            target=PurePosixPath("skills/public-skill"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            retires_replacements=("private:moved-to-public",),
        )
        retired_manifests = {
            MODULE.PUBLIC_OWNER: MODULE.ManifestData(
                owner=MODULE.PUBLIC_OWNER,
                entries=[keep],
                removed_links=[retirement],
            ),
            "private": private_manifest,
        }
        MODULE._validate_active_replacements(
            self.home,
            manifests,
            retired_manifests,
            [keep],
        )

    def test_runtime_rejects_self_replacement_retirement(self) -> None:
        removed = MODULE.RemovedLink(
            id="self-retirement",
            source=PurePosixPath("personal_codex/skills/old"),
            target=PurePosixPath("skills/old"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            replacement_target=PurePosixPath("skills/old"),
            retires_replacements=("public:self-retirement",),
        )
        manifest = MODULE.ManifestData(
            owner=MODULE.PUBLIC_OWNER,
            entries=[],
            removed_links=[removed],
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "must not retire its own replacement",
        ):
            MODULE._validate_active_replacements(
                self.home,
                {},
                {MODULE.PUBLIC_OWNER: manifest},
                [],
            )

    def test_runtime_rejects_replacement_retirement_cycles(self) -> None:
        def retirement(
            owner: str,
            node: str,
            replacement: str,
            retired_key: str,
        ) -> MODULE.RemovedLink:
            return MODULE.RemovedLink(
                id=node,
                source=PurePosixPath(f"personal_codex/skills/{node}"),
                target=PurePosixPath(f"skills/{node}"),
                kind="skill",
                owner=owner,
                replacement_target=PurePosixPath(f"skills/{replacement}"),
                retires_replacements=(retired_key,),
            )

        def manifests(
            removed_links: tuple[MODULE.RemovedLink, ...],
        ) -> dict[str, MODULE.ManifestData]:
            by_owner: dict[str, list[MODULE.RemovedLink]] = {}
            for entry in removed_links:
                by_owner.setdefault(entry.owner, []).append(entry)
            return {
                owner: MODULE.ManifestData(
                    owner=owner,
                    entries=[],
                    removed_links=entries,
                )
                for owner, entries in by_owner.items()
            }

        cases = (
            (
                "two-node",
                (
                    retirement(MODULE.PUBLIC_OWNER, "a", "b", "private:b"),
                    retirement("private", "b", "a", "public:a"),
                ),
            ),
            (
                "multi-node",
                (
                    retirement(MODULE.PUBLIC_OWNER, "a", "c", "private:b"),
                    retirement("private", "b", "a", "private:c"),
                    retirement("private", "c", "b", "public:a"),
                ),
            ),
        )

        for label, removed_links in cases:
            with self.subTest(cycle=label), self.assertRaisesRegex(
                MODULE.SyncError,
                "replacement retirement cycle",
            ):
                MODULE._validate_active_replacements(
                    self.home,
                    {},
                    manifests(removed_links),
                    [],
                )


class CurrentPointerSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _installed_home(self, name: str) -> tuple[Path, Path]:
        home = self.root / name
        release_root = home / "personal-sync" / "releases" / SHA_A
        write_skill_release(release_root)
        return home, release_root

    def test_accepts_only_canonical_relative_current_target(self) -> None:
        home, _release_root = self._installed_home("relative")
        current = home / "personal-sync" / "current"
        current.symlink_to(Path("releases") / SHA_A, target_is_directory=True)

        manifests = MODULE._installed_manifests(home)

        self.assertEqual(manifests[MODULE.PUBLIC_OWNER].owner, MODULE.PUBLIC_OWNER)
        self.assertEqual(MODULE._current_sha(home), SHA_A)

    def test_rejects_current_pointer_change_during_validation(self) -> None:
        home, _release_root = self._installed_home("concurrent-change")
        releases_root = home / "personal-sync" / "releases"
        (releases_root / SHA_B).mkdir()
        current = home / "personal-sync" / "current"
        current.symlink_to(Path("releases") / SHA_A, target_is_directory=True)

        release_a = releases_root / SHA_A
        real_resolve = MODULE.Path.resolve
        changed = False

        def resolve_and_change(path: Path, *, strict: bool = False) -> Path:
            nonlocal changed
            resolved = real_resolve(path, strict=strict)
            if path == release_a and not changed:
                changed = True
                current.unlink()
                current.symlink_to(
                    Path("releases") / SHA_B,
                    target_is_directory=True,
                )
            return resolved

        with mock.patch.object(MODULE.Path, "resolve", new=resolve_and_change):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "changed during validation",
            ):
                MODULE._current_sha(home)

    def test_rejects_non_exact_broken_or_unsafe_current_targets(self) -> None:
        cases = (
            "absolute",
            "descendant",
            "parent-alias",
            "missing",
            "symlink-release",
            "regular",
        )
        for case in cases:
            with self.subTest(case=case):
                home, release_root = self._installed_home(case)
                current = home / "personal-sync" / "current"
                if case == "absolute":
                    current.symlink_to(release_root, target_is_directory=True)
                elif case == "descendant":
                    (release_root / "nested").mkdir()
                    current.symlink_to(Path("releases") / SHA_A / "nested")
                elif case == "parent-alias":
                    current.symlink_to(
                        Path("..") / "personal-sync" / "releases" / SHA_A
                    )
                elif case == "missing":
                    current.symlink_to(Path("releases") / SHA_B)
                elif case == "symlink-release":
                    outside_release = self.root / "outside-release"
                    write_skill_release(outside_release)
                    for child in sorted(release_root.rglob("*"), reverse=True):
                        if child.is_dir():
                            child.rmdir()
                        else:
                            child.unlink()
                    release_root.rmdir()
                    release_root.symlink_to(outside_release, target_is_directory=True)
                    current.symlink_to(Path("releases") / SHA_A)
                else:
                    current.write_text("not a symlink\n", encoding="utf-8")

                with self.assertRaises(MODULE.SyncError):
                    MODULE._installed_manifests(home)
                with self.assertRaises(MODULE.SyncError):
                    MODULE.current_release_entries(home)

    def test_only_missing_current_is_treated_as_uninstalled(self) -> None:
        home, _release_root = self._installed_home("missing-current")

        self.assertEqual(MODULE._installed_manifests(home), {})
        self.assertEqual(MODULE.current_release_entries(home), [])


class InternalPathSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.manifest = write_skill_release(self.source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_beneath_helpers_reject_unsafe_components_before_open(self) -> None:
        home = self.root / "home-beneath"
        home.mkdir()
        outside = self.root / "outside-beneath"
        outside.mkdir()
        relative_home = Path(os.path.relpath(home, Path.cwd()))
        unsafe_paths = (
            (home, home / ".." / outside.name),
            (
                relative_home,
                relative_home / ".." / outside.name,
            ),
            (home, Path("")),
            (home, Path(".")),
        )

        for helper in (
            MODULE._open_directory_beneath,
            MODULE._open_or_create_directory_beneath,
        ):
            for root, unsafe_path in unsafe_paths:
                with self.subTest(helper=helper.__name__, path=unsafe_path):
                    with self.assertRaises(MODULE.SyncError):
                        helper(root, unsafe_path)

        self.assertEqual(list(outside.iterdir()), [])

    def test_home_symlink_is_rejected_without_external_writes(self) -> None:
        home = self.root / "home-symlink"
        outside = self.root / "outside-home-symlink"
        outside.mkdir()
        home.symlink_to(outside, target_is_directory=True)

        operations = (
            functools.partial(MODULE._open_directory_beneath, home, home),
            functools.partial(
                MODULE._open_or_create_directory_beneath,
                home,
                home / "personal-sync",
            ),
            functools.partial(
                MODULE._ensure_safe_internal_directory,
                home,
                home / "personal-sync" / "state",
                create=True,
            ),
            functools.partial(
                MODULE._capture_reconcile_target_snapshot,
                home,
                home / "skills" / "example",
            ),
            functools.partial(MODULE._load_pending_link_batch, home),
            functools.partial(
                MODULE._clear_pending_link_pointer,
                home,
                mock.Mock(),
            ),
            functools.partial(acquire_install_lock, home),
        )

        for operation in operations:
            with self.subTest(operation=operation.func.__name__):
                with self.assertRaisesRegex(MODULE.SyncError, "sync home"):
                    operation()

        pending_batch = mock.Mock()
        pending_batch.batch_root = home / "personal-sync" / "pending" / "batch"
        with self.assertRaisesRegex(MODULE.SyncError, "sync home"):
            MODULE._publish_pending_link_pointer(home, pending_batch)

        self.assertTrue(home.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_bound_home_reopen_rejects_concurrent_root_replacement(self) -> None:
        home = self.root / "home-bound-reopen"
        home.mkdir()
        displaced_home = self.root / "home-bound-reopen-displaced"
        home_fd = MODULE._open_sync_home(home)
        try:
            home.rename(displaced_home)
            home.mkdir()

            with self.assertRaisesRegex(MODULE.SyncError, "sync home changed"):
                MODULE._open_directory_beneath(home, home, home_fd=home_fd)
        finally:
            MODULE._close_fd_quietly(home_fd)

        self.assertEqual(list(home.iterdir()), [])
        self.assertEqual(list(displaced_home.iterdir()), [])

    def test_rejects_symlinked_internal_ancestors_without_external_writes(self) -> None:
        cases = (
            "personal-sync",
            "state",
            "overlays",
            "overlay-owner",
            "releases",
            "quarantine",
        )
        for case in cases:
            with self.subTest(case=case):
                home = self.root / f"home-{case}"
                home.mkdir()
                outside = self.root / f"outside-{case}"
                outside.mkdir()
                sync_root = home / "personal-sync"
                if case == "personal-sync":
                    sync_root.symlink_to(outside, target_is_directory=True)
                    operation = functools.partial(acquire_install_lock, home)
                else:
                    sync_root.mkdir()
                    if case == "state":
                        (sync_root / "state").symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        operation = functools.partial(
                            MODULE._write_managed_state,
                            home,
                            MODULE._empty_managed_state(),
                        )
                    elif case == "overlays":
                        (sync_root / "overlays").symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        operation = functools.partial(MODULE._known_owners, home)
                    elif case == "overlay-owner":
                        overlays = sync_root / "overlays"
                        overlays.mkdir()
                        (overlays / "private").symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        operation = functools.partial(
                            MODULE._current_sha,
                            home,
                            "private",
                        )
                    elif case == "releases":
                        (sync_root / "releases").symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        operation = functools.partial(
                            MODULE._stage_release_tree_for_install,
                            self.source,
                            home,
                            SHA_A,
                            self.manifest,
                        )
                    else:
                        (sync_root / "quarantine").symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        operation = functools.partial(
                            MODULE._quarantine_batch_root,
                            home,
                            [],
                        )

                with self.assertRaises(MODULE.SyncError):
                    operation()
                self.assertEqual(list(outside.iterdir()), [])


class AtomicMoveSafetyTests(unittest.TestCase):
    def test_destination_collision_does_not_overwrite_or_move_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            source = home / "skills" / "source"
            source.parent.mkdir(parents=True)
            source.symlink_to("original-source")
            destination = home / "quarantine" / "source"
            destination.parent.mkdir(parents=True)
            destination.write_text("concurrent destination\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                MODULE._atomic_move_beneath_home(home, source, destination)

            self.assertTrue(source.is_symlink())
            self.assertEqual(os.readlink(source), "original-source")
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "concurrent destination\n",
            )

    def test_destination_parent_replacement_is_rejected_before_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            source = home / "skills" / "source"
            source.parent.mkdir(parents=True)
            source.symlink_to("original-source")
            source_snapshot = MODULE._capture_reconcile_target_snapshot(home, source)
            destination = home / "quarantine" / "source"
            destination.parent.mkdir(parents=True)
            destination_parent_identity = (
                destination.parent.stat().st_dev,
                destination.parent.stat().st_ino,
            )
            displaced_parent = home / "quarantine-displaced"
            destination.parent.rename(displaced_parent)
            destination.parent.mkdir()

            with self.assertRaisesRegex(
                MODULE.SyncError,
                "destination parent changed after planning",
            ):
                MODULE._atomic_move_beneath_home(
                    home,
                    source,
                    destination,
                    source_snapshot,
                    destination_parent_identity,
                )

            self.assertTrue(source.is_symlink())
            self.assertEqual(os.readlink(source), "original-source")
            self.assertFalse(os.path.lexists(destination))

    def test_create_cleanup_restores_same_target_inode_racer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            target = home / "skills" / "moving"
            target.parent.mkdir(parents=True)
            real_bound_directory_matches = MODULE._bound_directory_matches
            real_rename_noreplace = MODULE._rename_noreplace_at
            target_parent_checks = 0
            raced = False
            racer_identity: tuple[int, int] | None = None

            def fail_second_target_parent_check(
                root: Path,
                directory: Path,
                directory_fd: int,
            ) -> bool:
                nonlocal target_parent_checks
                if directory == target.parent:
                    target_parent_checks += 1
                    return target_parent_checks == 1
                return real_bound_directory_matches(root, directory, directory_fd)

            def replace_leaf_before_move(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
            ) -> None:
                nonlocal raced, racer_identity
                if source_name == target.name and not raced:
                    raced = True
                    os.unlink(source_name, dir_fd=source_parent_fd)
                    os.symlink(
                        "created-source",
                        source_name,
                        dir_fd=source_parent_fd,
                    )
                    metadata = os.stat(
                        source_name,
                        dir_fd=source_parent_fd,
                        follow_symlinks=False,
                    )
                    racer_identity = (metadata.st_dev, metadata.st_ino)
                real_rename_noreplace(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )

            with (
                mock.patch.object(
                    MODULE,
                    "_bound_directory_matches",
                    side_effect=fail_second_target_parent_check,
                ),
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=replace_leaf_before_move,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "restored without replacement",
                ),
            ):
                MODULE._create_symlink_beneath(
                    home,
                    target,
                    "created-source",
                    "skill",
                )

            self.assertTrue(raced)
            self.assertTrue(target.is_symlink())
            self.assertEqual(os.readlink(target), "created-source")
            self.assertIsNotNone(racer_identity)
            self.assertEqual(
                (target.lstat().st_dev, target.lstat().st_ino),
                racer_identity,
            )
            quarantined = list(
                (home / "personal-sync" / "quarantine").glob("*/leaf/*")
            )
            self.assertEqual(quarantined, [])

    def test_remove_restores_same_target_inode_racer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            target = home / "skills" / "moving"
            target.parent.mkdir(parents=True)
            target.symlink_to("expected-source")
            real_rename_noreplace = MODULE._rename_noreplace_at
            raced = False
            racer_identity: tuple[int, int] | None = None

            def replace_leaf_before_move(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
            ) -> None:
                nonlocal raced, racer_identity
                if source_name == target.name and not raced:
                    raced = True
                    os.unlink(source_name, dir_fd=source_parent_fd)
                    os.symlink(
                        "expected-source",
                        source_name,
                        dir_fd=source_parent_fd,
                    )
                    metadata = os.stat(
                        source_name,
                        dir_fd=source_parent_fd,
                        follow_symlinks=False,
                    )
                    racer_identity = (metadata.st_dev, metadata.st_ino)
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
                    side_effect=replace_leaf_before_move,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "restored without replacement",
                ),
            ):
                MODULE._remove_expected_symlink_beneath(
                    home,
                    target,
                    "expected-source",
                )

            self.assertTrue(raced)
            self.assertTrue(target.is_symlink())
            self.assertEqual(os.readlink(target), "expected-source")
            self.assertIsNotNone(racer_identity)
            self.assertEqual(
                (target.lstat().st_dev, target.lstat().st_ino),
                racer_identity,
            )
            quarantined = list(
                (home / "personal-sync" / "quarantine").glob("*/leaf/*")
            )
            self.assertEqual(quarantined, [])

    def test_parent_swap_cannot_redirect_current_move_outside_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            owner_root = home / "personal-sync" / "overlays" / "private"
            owner_root.mkdir(parents=True)
            current = owner_root / "current"
            current.symlink_to("releases/original", target_is_directory=True)
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            outside_current = outside / "current"
            outside_current.symlink_to(
                "releases/external",
                target_is_directory=True,
            )
            backup = (
                home
                / "personal-sync"
                / "quarantine"
                / "batch"
                / "current"
                / "private"
            )
            backup.parent.mkdir(parents=True)
            moved_owner_root = owner_root.with_name("private-moved")
            real_open_directory = MODULE._open_directory_beneath
            swapped = False

            def open_then_swap(root: Path, directory: Path) -> int:
                nonlocal swapped
                directory_fd = real_open_directory(root, directory)
                if directory == owner_root and not swapped:
                    swapped = True
                    owner_root.rename(moved_owner_root)
                    owner_root.symlink_to(outside, target_is_directory=True)
                return directory_fd

            with mock.patch.object(
                MODULE,
                "_open_directory_beneath",
                side_effect=open_then_swap,
            ):
                with self.assertRaisesRegex(MODULE.SyncError, "source parent changed"):
                    MODULE._atomic_move_beneath_home(home, current, backup)

            self.assertFalse(os.path.lexists(backup))
            self.assertTrue(outside_current.is_symlink())
            self.assertEqual(os.readlink(outside_current), "releases/external")
            self.assertTrue((moved_owner_root / "current").is_symlink())
            self.assertEqual(
                os.readlink(moved_owner_root / "current"),
                "releases/original",
            )

    def test_reconcile_does_not_write_through_swapped_target_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            skills = home / "skills"
            skills.mkdir(parents=True)
            target = skills / "moving"
            target.symlink_to("old-source")
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            moved_skills = Path(temp_dir) / "moved-skills"
            action = planned_reconcile_action(
                home,
                "replace",
                target,
                "new-source",
                "skill",
                expected_link_target="old-source",
            )
            skills.rename(moved_skills)
            skills.mkdir()

            with self.assertRaises(MODULE.SyncError):
                MODULE._apply_reconcile_actions(home, [action], dry_run=False)

            self.assertFalse(os.path.lexists(outside / "moving"))
            self.assertTrue((moved_skills / "moving").is_symlink())
            self.assertEqual(os.readlink(moved_skills / "moving"), "old-source")
            self.assertFalse(os.path.lexists(skills / "moving"))
            quarantine = home / "personal-sync" / "quarantine"
            backups = [
                batch / "links" / "skills" / "moving"
                for batch in quarantine.iterdir()
                if batch.is_dir()
                and os.path.lexists(batch / "links" / "skills" / "moving")
            ]
            self.assertEqual(backups, [])


class ReconcileTransactionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_atomic_move_preserves_unexpected_regular_file(self) -> None:
        target = self.home / "skills" / "old"
        target.parent.mkdir(parents=True)
        target.symlink_to("old-source")
        action = planned_reconcile_action(
            self.home,
            "remove",
            target,
            "",
            "skill",
            expected_link_target="old-source",
        )
        real_move = MODULE._atomic_move_beneath_home
        raced = False

        def move_after_race(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            nonlocal raced
            if source == target and not raced:
                raced = True
                target.unlink()
                target.write_text("unmanaged\n", encoding="utf-8")
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=move_after_race,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "source changed after planning",
            ):
                MODULE._apply_reconcile_actions(self.home, [action], dry_run=False)

        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "unmanaged\n")
        backups = list(
            (self.home / "personal-sync" / "quarantine").glob(
                "*/links/skills/old"
            )
        )
        self.assertEqual(backups, [])

    def test_same_target_inode_racer_is_moved_back_after_quarantine(self) -> None:
        target = self.home / "skills" / "old"
        target.parent.mkdir(parents=True)
        target.symlink_to("old-source")
        action = planned_reconcile_action(
            self.home,
            "remove",
            target,
            "",
            "skill",
            expected_link_target="old-source",
        )
        real_rename = MODULE._rename_noreplace_at
        racer_identity: tuple[int, int] | None = None

        def replace_in_rename_window(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal racer_identity
            if source_name == target.name and racer_identity is None:
                os.unlink(source_name, dir_fd=source_parent_fd)
                os.symlink("old-source", source_name, dir_fd=source_parent_fd)
                metadata = os.stat(
                    source_name,
                    dir_fd=source_parent_fd,
                    follow_symlinks=False,
                )
                racer_identity = (metadata.st_dev, metadata.st_ino)
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with mock.patch.object(
            MODULE,
            "_rename_noreplace_at",
            side_effect=replace_in_rename_window,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "moved entry changed during reconciliation",
            ):
                MODULE._apply_reconcile_actions(self.home, [action], dry_run=False)

        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), "old-source")
        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            racer_identity,
        )
        backups = list(
            (self.home / "personal-sync" / "quarantine").glob(
                "*/links/skills/old"
            )
        )
        self.assertEqual(backups, [])

    def test_changed_backup_is_preserved_and_reports_incomplete_rollback(self) -> None:
        target = self.home / "skills" / "old"
        target.parent.mkdir(parents=True)
        target.symlink_to("old-source")
        action = planned_reconcile_action(
            self.home,
            "replace",
            target,
            "new-source",
            "skill",
            expected_link_target="old-source",
        )

        def corrupt_backup(
            home_to_verify: Path,
            actions: list[MODULE.ReconcileAction],
        ) -> None:
            self.assertEqual(home_to_verify, self.home)
            if not actions:
                return
            quarantine = self.home / "personal-sync" / "quarantine"
            backups = [
                batch / "links" / "skills" / "old"
                for batch in quarantine.iterdir()
                if batch.is_dir()
                and os.path.lexists(batch / "links" / "skills" / "old")
            ]
            self.assertEqual(len(backups), 1)
            backups[0].unlink()
            backups[0].write_text("concurrent backup drift\n", encoding="utf-8")
            raise MODULE.SyncError("injected post-replace failure")

        with mock.patch.object(
            MODULE,
            "_verify_reconcile_action_targets",
            side_effect=corrupt_backup,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "backup changed and was retained",
            ):
                MODULE._apply_reconcile_actions(self.home, [action], dry_run=False)

        self.assertFalse(os.path.lexists(target))
        backups = list(
            (self.home / "personal-sync" / "quarantine").glob(
                "*/links/skills/old"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].is_file())
        self.assertFalse(backups[0].is_symlink())
        self.assertEqual(
            backups[0].read_text(encoding="utf-8"),
            "concurrent backup drift\n",
        )

    def test_current_switch_preserves_concurrent_regular_file(self) -> None:
        sync_root = self.home / "personal-sync"
        sync_root.mkdir()
        current = sync_root / "current"
        current.symlink_to(f"releases/{SHA_A}", target_is_directory=True)
        real_move = MODULE._atomic_move_beneath_home
        raced = False

        def move_after_race(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            nonlocal raced
            if source == current and not raced:
                raced = True
                current.unlink()
                current.write_text("concurrent current\n", encoding="utf-8")
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=move_after_race,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "source changed after planning",
            ):
                MODULE._switch_current(self.home, SHA_B, dry_run=False)

        self.assertTrue(current.is_file())
        self.assertEqual(current.read_text(encoding="utf-8"), "concurrent current\n")
        backups = list(
            (sync_root / "quarantine").glob(
                "*/links/personal-sync/current"
            )
        )
        self.assertEqual(backups, [])

    def test_current_switch_rejects_same_target_inode_replacement(self) -> None:
        sync_root = self.home / "personal-sync"
        sync_root.mkdir()
        current = sync_root / "current"
        old_target = f"releases/{SHA_A}"
        current.symlink_to(old_target, target_is_directory=True)
        real_move = MODULE._atomic_move_beneath_home
        racer_identity: tuple[int, int] | None = None

        def replace_before_move(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            nonlocal racer_identity
            if source == current and racer_identity is None:
                current.unlink()
                current.symlink_to(old_target, target_is_directory=True)
                racer_identity = (current.lstat().st_dev, current.lstat().st_ino)
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=replace_before_move,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "source changed after planning"):
                MODULE._switch_current(self.home, SHA_B, dry_run=False)

        self.assertEqual(os.readlink(current), old_target)
        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (current.lstat().st_dev, current.lstat().st_ino),
            racer_identity,
        )

    def test_current_switch_rejects_parent_replacement_after_planning(self) -> None:
        sync_root = self.home / "personal-sync"
        sync_root.mkdir()
        current = sync_root / "current"
        current.symlink_to(f"releases/{SHA_A}", target_is_directory=True)
        displaced_root = self.home / "personal-sync-displaced"
        real_move = MODULE._atomic_move_beneath_home
        raced = False

        def replace_parent_before_move(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            nonlocal raced
            if source == current and not raced:
                raced = True
                sync_root.rename(displaced_root)
                sync_root.mkdir()
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=replace_parent_before_move,
        ):
            with self.assertRaises(MODULE.SyncError):
                MODULE._switch_current(self.home, SHA_B, dry_run=False)

        self.assertTrue(raced)
        self.assertFalse(os.path.lexists(current))
        displaced_current = displaced_root / "current"
        self.assertTrue(displaced_current.is_symlink())
        self.assertEqual(os.readlink(displaced_current), f"releases/{SHA_A}")

    def test_current_rollback_preserves_concurrent_pointer(self) -> None:
        sync_root = self.home / "personal-sync"
        sync_root.mkdir()
        current = sync_root / "current"
        current.symlink_to(f"releases/{SHA_A}", target_is_directory=True)
        action = MODULE._plan_current_switch_action(self.home, SHA_B)
        self.assertIsNotNone(action)
        transaction = MODULE._apply_reconcile_actions(
            self.home,
            [action],
            dry_run=False,
        )
        current.unlink()
        current.symlink_to("releases/concurrent", target_is_directory=True)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "refusing to remove changed managed symlink",
        ):
            MODULE._rollback_reconcile_transaction(self.home, transaction)

        self.assertTrue(current.is_symlink())
        self.assertEqual(os.readlink(current), "releases/concurrent")
        assert transaction is not None
        assert transaction.batch_root is not None
        original_backup = (
            transaction.batch_root / "links" / "personal-sync" / "current"
        )
        self.assertEqual(os.readlink(original_backup), f"releases/{SHA_A}")
        self.assertFalse((transaction.batch_root / "rollback").exists())

    def test_rollback_retains_backup_when_planned_parent_is_replaced(self) -> None:
        skills = self.home / "skills"
        skills.mkdir()
        target = skills / "example"
        target.symlink_to("old-source")
        action = planned_reconcile_action(
            self.home,
            "replace",
            target,
            "new-source",
            "skill",
            expected_link_target="old-source",
        )
        transaction = MODULE._apply_reconcile_actions(
            self.home,
            [action],
            dry_run=False,
        )
        assert transaction is not None
        assert transaction.batch_root is not None
        backup = transaction.batch_root / "links" / "skills" / "example"
        displaced_skills = self.home / "skills-displaced"
        skills.rename(displaced_skills)
        skills.mkdir()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "changed target parent",
        ):
            MODULE._rollback_reconcile_transaction(self.home, transaction)

        self.assertFalse(os.path.lexists(target))
        self.assertTrue((displaced_skills / "example").is_symlink())
        self.assertEqual(os.readlink(displaced_skills / "example"), "new-source")
        self.assertTrue(backup.is_symlink())
        self.assertEqual(os.readlink(backup), "old-source")

    def test_second_destructive_failure_rolls_back_first_replace(self) -> None:
        first = self.home / "skills" / "first"
        second = self.home / "skills" / "second"
        first.parent.mkdir(parents=True)
        first.symlink_to("old-first")
        second.write_text("unmanaged\n", encoding="utf-8")
        actions = [
            planned_reconcile_action(
                self.home,
                "replace",
                first,
                "new-first",
                "skill",
                expected_link_target="old-first",
            ),
            planned_reconcile_action(
                self.home,
                "replace",
                second,
                "new-second",
                "skill",
                expected_link_target="old-second",
            ),
        ]

        with self.assertRaises(MODULE.SyncError):
            MODULE._apply_reconcile_actions(self.home, actions, dry_run=False)

        self.assertTrue(first.is_symlink())
        self.assertEqual(os.readlink(first), "old-first")
        self.assertTrue(second.is_file())
        self.assertEqual(second.read_text(encoding="utf-8"), "unmanaged\n")
        second_backups = list(
            (self.home / "personal-sync" / "quarantine").glob(
                "*/links/skills/second"
            )
        )
        self.assertEqual(second_backups, [])

    def test_revalidates_each_removals_replacement_and_rolls_back_batch(self) -> None:
        skills = self.home / "skills"
        skills.mkdir()
        old_one = skills / "old-one"
        old_two = skills / "old-two"
        new_one = skills / "new-one"
        new_two = skills / "new-two"
        old_one.symlink_to("old-one-source")
        old_two.symlink_to("old-two-source")
        replacement_one = MODULE.LinkEntry(
            source=PurePosixPath("personal_codex/skills/new-one"),
            target=PurePosixPath("skills/new-one"),
            kind="skill",
        )
        replacement_two = MODULE.LinkEntry(
            source=PurePosixPath("personal_codex/skills/new-two"),
            target=PurePosixPath("skills/new-two"),
            kind="skill",
        )
        new_one.symlink_to(MODULE._desired_link_target(self.home, replacement_one))
        new_two.symlink_to(MODULE._desired_link_target(self.home, replacement_two))
        actions = [
            planned_reconcile_action(
                self.home,
                "remove",
                old_one,
                "",
                "skill",
                expected_link_target="old-one-source",
            ),
            planned_reconcile_action(
                self.home,
                "remove",
                old_two,
                "",
                "skill",
                expected_link_target="old-two-source",
            ),
        ]
        required = {
            old_one: [replacement_one],
            old_two: [replacement_two],
        }
        real_move = MODULE._atomic_move_beneath_home
        drifted = False

        def drift_second_replacement(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            nonlocal drifted
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )
            if source == old_one and not drifted:
                drifted = True
                new_two.unlink()
                new_two.symlink_to("concurrent-drift")

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=drift_second_replacement,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "active replacement target changed before removal",
            ):
                MODULE._apply_reconcile_actions(
                    self.home,
                    actions,
                    dry_run=False,
                    required_replacements=required,
                )

        self.assertEqual(os.readlink(old_one), "old-one-source")
        self.assertEqual(os.readlink(old_two), "old-two-source")
        self.assertEqual(os.readlink(new_two), "concurrent-drift")


class ManagedStateReadSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.state_path = (
            self.home / "personal-sync" / "state" / "managed-links.json"
        )
        self.state_path.parent.mkdir(parents=True)
        self.original_payload = (
            json.dumps({"version": 1, "owners": {}, "links": []}) + "\n"
        ).encode("utf-8")
        self.state_path.write_bytes(self.original_payload)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rejects_regular_file_replacement_between_stat_and_open(self) -> None:
        original_path = self.state_path.with_suffix(".before")
        replacement_payload = (
            json.dumps({"version": 1, "owners": {}, "links": [], "raced": True})
            + "\n"
        ).encode("utf-8")
        real_open = MODULE.os.open
        replaced = False

        def open_after_replacement(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if path == self.state_path.name and dir_fd is not None and not replaced:
                replaced = True
                self.state_path.rename(original_path)
                self.state_path.write_bytes(replacement_payload)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(
            MODULE.os,
            "open",
            side_effect=open_after_replacement,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed sync state changed before read",
            ):
                MODULE._load_managed_state(self.home)

        self.assertTrue(replaced)
        self.assertEqual(self.state_path.read_bytes(), replacement_payload)
        self.assertEqual(original_path.read_bytes(), self.original_payload)

    def test_rejects_parent_replacement_during_read(self) -> None:
        original_parent = self.state_path.parent
        moved_parent = original_parent.with_name("state-before-swap")
        replacement_payload = (
            json.dumps({"version": 1, "owners": {}, "links": [], "raced": True})
            + "\n"
        ).encode("utf-8")
        real_read = MODULE._read_managed_state_bytes
        replaced = False

        def read_then_replace_parent(file_fd: int, path: Path) -> bytes:
            nonlocal replaced
            payload = real_read(file_fd, path)
            original_parent.rename(moved_parent)
            original_parent.mkdir()
            self.state_path.write_bytes(replacement_payload)
            replaced = True
            return payload

        with mock.patch.object(
            MODULE,
            "_read_managed_state_bytes",
            side_effect=read_then_replace_parent,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed sync state parent changed during read",
            ):
                MODULE._load_managed_state(self.home)

        self.assertTrue(replaced)
        self.assertEqual(self.state_path.read_bytes(), replacement_payload)
        self.assertEqual(
            (moved_parent / self.state_path.name).read_bytes(),
            self.original_payload,
        )

    def test_rejects_fifo_replacement_without_blocking_or_reading(self) -> None:
        original_path = self.state_path.with_suffix(".before-fifo")
        real_open = MODULE.os.open
        replaced = False

        def open_after_fifo_replacement(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if path == self.state_path.name and dir_fd is not None and not replaced:
                replaced = True
                self.state_path.rename(original_path)
                os.mkfifo(self.state_path, mode=0o600)
                self.assertNotEqual(flags & getattr(os, "O_NONBLOCK", 0), 0)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            mock.patch.object(
                MODULE.os,
                "open",
                side_effect=open_after_fifo_replacement,
            ),
            mock.patch.object(MODULE.os, "read") as read_file,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "refusing non-file sync state",
            ):
                MODULE._load_managed_state(self.home)

        self.assertTrue(replaced)
        self.assertTrue(self.state_path.is_fifo())
        self.assertEqual(original_path.read_bytes(), self.original_payload)
        read_file.assert_not_called()

    def test_does_not_follow_symlink_replacement(self) -> None:
        original_path = self.state_path.with_suffix(".before-symlink")
        real_open = MODULE.os.open
        replaced = False

        def open_after_symlink_replacement(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if path == self.state_path.name and dir_fd is not None and not replaced:
                replaced = True
                self.state_path.rename(original_path)
                self.state_path.symlink_to(os.devnull)
                self.assertNotEqual(flags & getattr(os, "O_NOFOLLOW", 0), 0)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            mock.patch.object(
                MODULE.os,
                "open",
                side_effect=open_after_symlink_replacement,
            ),
            mock.patch.object(MODULE.os, "read") as read_file,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "Failed to read"):
                MODULE._load_managed_state(self.home)

        self.assertTrue(replaced)
        self.assertTrue(self.state_path.is_symlink())
        self.assertEqual(os.readlink(self.state_path), os.devnull)
        self.assertEqual(original_path.read_bytes(), self.original_payload)
        read_file.assert_not_called()

    def test_rejects_device_descriptor_before_reading(self) -> None:
        real_open = MODULE.os.open
        device_fd = real_open(os.devnull, os.O_RDONLY)
        returned_device = False

        def open_as_device(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal returned_device
            if (
                path == self.state_path.name
                and dir_fd is not None
                and not returned_device
            ):
                returned_device = True
                return os.dup(device_fd)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        try:
            with (
                mock.patch.object(MODULE.os, "open", side_effect=open_as_device),
                mock.patch.object(MODULE.os, "read") as read_file,
            ):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "refusing non-file sync state",
                ):
                    MODULE._load_managed_state(self.home)
        finally:
            os.close(device_fd)

        self.assertTrue(returned_device)
        self.assertEqual(self.state_path.read_bytes(), self.original_payload)
        read_file.assert_not_called()

    def test_rejects_oversized_state_before_reading(self) -> None:
        with self.state_path.open("wb") as state_file:
            state_file.truncate(MODULE.MAX_MANAGED_STATE_BYTES + 1)

        with mock.patch.object(MODULE.os, "read") as read_file:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed link state exceeds",
            ):
                MODULE._load_managed_state(self.home)

        read_file.assert_not_called()


class ManagedStateTransactionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.state_path = (
            self.home / "personal-sync" / "state" / "managed-links.json"
        )
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_bytes(b"before\n")
        self.state_path.chmod(0o640)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_snapshot_rejects_fifo_replacement_without_blocking(self) -> None:
        original_path = self.state_path.with_suffix(".before-fifo")
        real_open = MODULE.os.open
        replaced = False

        def open_after_fifo_replacement(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if path == self.state_path.name and dir_fd is not None and not replaced:
                replaced = True
                self.state_path.rename(original_path)
                os.mkfifo(self.state_path, mode=0o600)
                self.assertNotEqual(flags & getattr(os, "O_NONBLOCK", 0), 0)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            mock.patch.object(
                MODULE.os,
                "open",
                side_effect=open_after_fifo_replacement,
            ),
            mock.patch.object(MODULE.os, "read") as read_file,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "refusing non-file sync state",
            ):
                MODULE._snapshot_managed_state_file(self.home)

        self.assertTrue(replaced)
        self.assertTrue(self.state_path.is_fifo())
        self.assertEqual(original_path.read_bytes(), b"before\n")
        read_file.assert_not_called()

    def test_snapshot_rejects_oversized_state_before_reading(self) -> None:
        with self.state_path.open("wb") as state_file:
            state_file.truncate(MODULE.MAX_MANAGED_STATE_BYTES + 1)

        with mock.patch.object(MODULE.os, "read") as read_file:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed link state exceeds",
            ):
                MODULE._snapshot_managed_state_file(self.home)

        read_file.assert_not_called()

    def test_snapshot_rejects_parent_replacement_during_read(self) -> None:
        original_parent = self.state_path.parent
        moved_parent = original_parent.with_name("state-before-snapshot-swap")
        real_read = MODULE._read_managed_state_bytes
        replaced = False

        def read_then_replace_parent(file_fd: int, path: Path) -> bytes:
            nonlocal replaced
            payload = real_read(file_fd, path)
            original_parent.rename(moved_parent)
            original_parent.mkdir()
            self.state_path.write_bytes(b"replacement\n")
            replaced = True
            return payload

        with mock.patch.object(
            MODULE,
            "_read_managed_state_bytes",
            side_effect=read_then_replace_parent,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed sync state parent changed during read",
            ):
                MODULE._snapshot_managed_state_file(self.home)

        self.assertTrue(replaced)
        self.assertEqual(self.state_path.read_bytes(), b"replacement\n")
        self.assertEqual(
            (moved_parent / self.state_path.name).read_bytes(),
            b"before\n",
        )

    def test_match_rejects_fifo_without_blocking(self) -> None:
        snapshot = MODULE._snapshot_managed_state_file(self.home)
        self.state_path.unlink()
        os.mkfifo(self.state_path, mode=0o600)

        with mock.patch.object(MODULE.os, "read") as read_file:
            self.assertFalse(
                MODULE._managed_state_file_matches(
                    self.home,
                    self.state_path,
                    snapshot,
                )
            )

        self.assertTrue(self.state_path.is_fifo())
        read_file.assert_not_called()

    def test_match_rejects_oversized_state_before_reading(self) -> None:
        snapshot = MODULE._snapshot_managed_state_file(self.home)
        with self.state_path.open("wb") as state_file:
            state_file.truncate(MODULE.MAX_MANAGED_STATE_BYTES + 1)

        with mock.patch.object(MODULE.os, "read") as read_file:
            self.assertFalse(
                MODULE._managed_state_file_matches(
                    self.home,
                    self.state_path,
                    snapshot,
                )
            )

        read_file.assert_not_called()

    def test_match_rejects_parent_replacement_during_read(self) -> None:
        snapshot = MODULE._snapshot_managed_state_file(self.home)
        original_parent = self.state_path.parent
        moved_parent = original_parent.with_name("state-before-match-swap")
        real_read = MODULE._read_managed_state_bytes
        replaced = False

        def read_then_replace_parent(file_fd: int, path: Path) -> bytes:
            nonlocal replaced
            payload = real_read(file_fd, path)
            original_parent.rename(moved_parent)
            original_parent.mkdir()
            self.state_path.write_bytes(b"before\n")
            self.state_path.chmod(0o640)
            replaced = True
            return payload

        with mock.patch.object(
            MODULE,
            "_read_managed_state_bytes",
            side_effect=read_then_replace_parent,
        ):
            self.assertFalse(
                MODULE._managed_state_file_matches(
                    self.home,
                    self.state_path,
                    snapshot,
                )
            )

        self.assertTrue(replaced)
        self.assertEqual(self.state_path.read_bytes(), b"before\n")
        self.assertEqual(
            (moved_parent / self.state_path.name).read_bytes(),
            b"before\n",
        )

    def test_identical_state_write_retains_original_in_quarantine(self) -> None:
        self.state_path.unlink()
        state = MODULE._empty_managed_state()
        MODULE._write_managed_state(self.home, state)
        before = self.state_path.read_bytes()
        MODULE._write_managed_state(self.home, state)

        self.assertEqual(self.state_path.read_bytes(), before)
        backups = list(
            (self.home / "personal-sync" / "quarantine").glob(
                "*/state/managed-links.json"
            )
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertNotEqual(backups[0].stat().st_ino, self.state_path.stat().st_ino)

    def test_publish_does_not_overwrite_concurrent_state(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        real_rename = MODULE._rename_noreplace_at
        raced = False

        def rename_after_race(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal raced
            if (
                destination_name == self.state_path.name
                and ".publish." in source_name
                and not raced
            ):
                raced = True
                self.state_path.write_bytes(b"concurrent\n")
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with mock.patch.object(
            MODULE,
            "_rename_noreplace_at",
            side_effect=rename_after_race,
        ):
            with self.assertRaises(FileExistsError):
                MODULE._write_managed_state(
                    self.home,
                    state,
                    transaction,
                )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "left in place during rollback",
        ):
            MODULE._restore_managed_state_file(self.home, transaction)

        self.assertEqual(self.state_path.read_bytes(), b"concurrent\n")

    def test_publish_preserves_same_content_temp_inode_racer(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        published_payload = MODULE._managed_state_bytes(state)
        real_rename = MODULE._rename_noreplace_at
        racer_identity: tuple[int, int] | None = None

        def replace_publish_temp_before_rename(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal racer_identity
            if (
                destination_name == self.state_path.name
                and ".publish." in source_name
                and racer_identity is None
            ):
                os.unlink(source_name, dir_fd=source_parent_fd)
                file_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(file_fd, published_payload)
                    os.fchmod(file_fd, 0o600)
                    metadata = os.fstat(file_fd)
                    racer_identity = (metadata.st_dev, metadata.st_ino)
                finally:
                    os.close(file_fd)
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with mock.patch.object(
            MODULE,
            "_rename_noreplace_at",
            side_effect=replace_publish_temp_before_rename,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed state entry changed before quarantine",
            ):
                MODULE._write_managed_state(self.home, state, transaction)

        self.assertEqual(self.state_path.read_bytes(), published_payload)
        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            racer_identity,
        )
        assert transaction.backup is not None
        self.assertEqual(transaction.backup.read_bytes(), b"before\n")
        self.assertEqual(transaction.backup.stat().st_mode & 0o777, 0o640)

    def test_publish_parent_swap_does_not_touch_new_canonical_state(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        original_parent = self.state_path.parent
        moved_parent = original_parent.with_name("state-moved")
        real_fsync = MODULE._fsync_directory
        swapped = False
        canonical_identity: tuple[int, int] | None = None

        def fsync_then_swap(
            path: Path,
            directory_fd: int | None = None,
        ) -> None:
            nonlocal canonical_identity, swapped
            real_fsync(path, directory_fd)
            if swapped:
                return
            swapped = True
            original_parent.rename(moved_parent)
            original_parent.mkdir()
            self.state_path.write_bytes(MODULE._managed_state_bytes(state))
            self.state_path.chmod(0o600)
            metadata = self.state_path.stat()
            canonical_identity = (metadata.st_dev, metadata.st_ino)

        with mock.patch.object(
            MODULE,
            "_fsync_directory",
            side_effect=fsync_then_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "parent changed",
            ):
                MODULE._write_managed_state(
                    self.home,
                    state,
                    transaction,
                )

        self.assertEqual(
            self.state_path.read_bytes(),
            MODULE._managed_state_bytes(state),
        )
        self.assertIsNotNone(canonical_identity)
        metadata = self.state_path.stat()
        self.assertEqual(
            (metadata.st_dev, metadata.st_ino),
            canonical_identity,
        )
        self.assertFalse((moved_parent / self.state_path.name).exists())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "parent changed during rollback",
        ):
            MODULE._restore_managed_state_file(self.home, transaction)
        self.assertEqual(
            self.state_path.read_bytes(),
            MODULE._managed_state_bytes(state),
        )

    def test_publish_rejects_change_before_final_verification(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        real_fsync = MODULE._fsync_directory

        def fsync_then_change(
            path: Path,
            directory_fd: int | None = None,
        ) -> None:
            real_fsync(path, directory_fd)
            self.state_path.write_bytes(b"changed before final verification\n")

        with mock.patch.object(
            MODULE,
            "_fsync_directory",
            side_effect=fsync_then_change,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "published sync state changed",
            ):
                MODULE._write_managed_state(
                    self.home,
                    state,
                    transaction,
                )

        MODULE._restore_managed_state_file(self.home, transaction)
        self.assertEqual(self.state_path.read_bytes(), b"before\n")
        assert transaction.batch_root is not None
        bad_publications = list(
            (transaction.batch_root / "state").glob("publish-error-*")
        )
        self.assertEqual(len(bad_publications), 1)
        self.assertEqual(
            bad_publications[0].read_bytes(),
            b"changed before final verification\n",
        )

    def test_rollback_preserves_changed_published_state(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        MODULE._write_managed_state(self.home, state, transaction)
        self.state_path.write_bytes(b"concurrent published state\n")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "restored without replacement",
        ):
            MODULE._restore_managed_state_file(self.home, transaction)

        self.assertEqual(
            self.state_path.read_bytes(),
            b"concurrent published state\n",
        )
        assert transaction.backup is not None
        self.assertEqual(transaction.backup.read_bytes(), b"before\n")
        assert transaction.batch_root is not None
        rollback_backups = list(
            (transaction.batch_root / "state").glob("rollback-current-*")
        )
        self.assertEqual(rollback_backups, [])

    def test_rollback_preserves_same_content_replacement_inode(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        MODULE._write_managed_state(self.home, state, transaction)
        displaced_publication = self.state_path.with_suffix(".published-before-swap")
        published_payload = self.state_path.read_bytes()
        published_mode = self.state_path.stat().st_mode & 0o777
        self.state_path.rename(displaced_publication)
        self.state_path.write_bytes(published_payload)
        self.state_path.chmod(published_mode)
        replacement_identity = (
            self.state_path.stat().st_dev,
            self.state_path.stat().st_ino,
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed state entry changed before quarantine",
        ):
            MODULE._restore_managed_state_file(self.home, transaction)

        self.assertEqual(self.state_path.read_bytes(), published_payload)
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            replacement_identity,
        )
        self.assertEqual(displaced_publication.read_bytes(), published_payload)
        self.assertNotEqual(
            (
                displaced_publication.stat().st_dev,
                displaced_publication.stat().st_ino,
            ),
            replacement_identity,
        )
        assert transaction.backup is not None
        self.assertEqual(transaction.backup.read_bytes(), b"before\n")

    def test_rollback_cleanup_restores_same_content_inode_racer(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        MODULE._write_managed_state(self.home, state, transaction)
        published_payload = self.state_path.read_bytes()
        published_mode = self.state_path.stat().st_mode & 0o777
        real_rename = MODULE._rename_noreplace_at
        racer_identity: tuple[int, int] | None = None

        def replace_in_quarantine_window(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal racer_identity
            if (
                source_name == self.state_path.name
                and destination_name.startswith("rollback-current-")
                and racer_identity is None
            ):
                os.unlink(source_name, dir_fd=source_parent_fd)
                file_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    published_mode,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(file_fd, published_payload)
                    os.fchmod(file_fd, published_mode)
                    metadata = os.fstat(file_fd)
                    racer_identity = (metadata.st_dev, metadata.st_ino)
                finally:
                    os.close(file_fd)
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with mock.patch.object(
            MODULE,
            "_rename_noreplace_at",
            side_effect=replace_in_quarantine_window,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "restored without replacement",
            ):
                MODULE._restore_managed_state_file(self.home, transaction)

        self.assertEqual(self.state_path.read_bytes(), published_payload)
        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            racer_identity,
        )
        assert transaction.batch_root is not None
        self.assertEqual(
            list((transaction.batch_root / "state").glob("rollback-current-*")),
            [],
        )

    def test_restore_uses_independent_inode(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        MODULE._write_managed_state(self.home, state, transaction)
        MODULE._restore_managed_state_file(self.home, transaction)

        assert transaction.backup is not None
        self.assertEqual(self.state_path.read_bytes(), b"before\n")
        self.assertNotEqual(
            self.state_path.stat().st_ino,
            transaction.backup.stat().st_ino,
        )
        self.state_path.write_bytes(b"edited live state\n")
        self.assertEqual(transaction.backup.read_bytes(), b"before\n")

    def test_restore_detects_backup_change_during_publish(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        MODULE._write_managed_state(self.home, state, transaction)
        assert transaction.backup is not None
        real_fsync = MODULE._fsync_directory

        def change_backup_after_fsync(
            path: Path,
            directory_fd: int | None = None,
        ) -> None:
            real_fsync(path, directory_fd)
            transaction.backup.write_bytes(b"changed backup\n")

        with mock.patch.object(
            MODULE,
            "_fsync_directory",
            side_effect=change_backup_after_fsync,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "original sync state changed",
            ):
                MODULE._restore_managed_state_file(self.home, transaction)

        self.assertFalse(os.path.lexists(self.state_path))
        self.assertEqual(transaction.backup.read_bytes(), b"changed backup\n")
        assert transaction.batch_root is not None
        restored_backups = list(
            (transaction.batch_root / "state").glob("restore-error-*")
        )
        self.assertEqual(len(restored_backups), 1)
        self.assertEqual(restored_backups[0].read_bytes(), b"before\n")
        self.assertNotEqual(
            restored_backups[0].stat().st_ino,
            transaction.backup.stat().st_ino,
        )

    def test_restore_rejects_change_before_final_verification(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        MODULE._write_managed_state(self.home, state, transaction)
        real_fsync = MODULE._fsync_directory

        def fsync_then_change(
            path: Path,
            directory_fd: int | None = None,
        ) -> None:
            real_fsync(path, directory_fd)
            self.state_path.write_bytes(b"changed restored state\n")

        with mock.patch.object(
            MODULE,
            "_fsync_directory",
            side_effect=fsync_then_change,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "restored sync state changed after fsync",
            ):
                MODULE._restore_managed_state_file(self.home, transaction)

        self.assertFalse(os.path.lexists(self.state_path))
        assert transaction.batch_root is not None
        restored_backups = list(
            (transaction.batch_root / "state").glob("restore-error-*")
        )
        self.assertEqual(len(restored_backups), 1)
        self.assertEqual(
            restored_backups[0].read_bytes(),
            b"changed restored state\n",
        )

    def test_restore_parent_swap_does_not_touch_new_canonical_state(self) -> None:
        state = MODULE._empty_managed_state()
        transaction = MODULE._prepare_managed_state_transaction(
            self.home,
            state,
        )
        MODULE._write_managed_state(self.home, state, transaction)
        original_parent = self.state_path.parent
        moved_parent = original_parent.with_name("state-moved")
        real_fsync = MODULE._fsync_directory
        swapped = False
        canonical_identity: tuple[int, int] | None = None

        def fsync_then_swap(
            path: Path,
            directory_fd: int | None = None,
        ) -> None:
            nonlocal canonical_identity, swapped
            real_fsync(path, directory_fd)
            if swapped:
                return
            swapped = True
            original_parent.rename(moved_parent)
            original_parent.mkdir()
            self.state_path.write_bytes(b"before\n")
            self.state_path.chmod(0o640)
            metadata = self.state_path.stat()
            canonical_identity = (metadata.st_dev, metadata.st_ino)

        with mock.patch.object(
            MODULE,
            "_fsync_directory",
            side_effect=fsync_then_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "parent changed",
            ):
                MODULE._restore_managed_state_file(self.home, transaction)

        self.assertEqual(
            self.state_path.read_bytes(),
            b"before\n",
        )
        self.assertIsNotNone(canonical_identity)
        metadata = self.state_path.stat()
        self.assertEqual(
            (metadata.st_dev, metadata.st_ino),
            canonical_identity,
        )
        self.assertFalse((moved_parent / self.state_path.name).exists())
        assert transaction.backup is not None
        self.assertEqual(transaction.backup.read_bytes(), b"before\n")


class InstallTransactionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.release_a = self.root / "release-a"
        self.release_b = self.root / "release-b"
        write_skill_release(self.release_a, source_name="old")
        write_skill_release(self.release_b, source_name="new")
        install_quietly(self.release_a, self.home, SHA_A)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _snapshot(self) -> tuple[str, str, bytes, int]:
        current = self.home / "personal-sync" / "current"
        target = self.home / "skills" / "example"
        state = self.home / "personal-sync" / "state" / "managed-links.json"
        return (
            os.readlink(current),
            os.readlink(target),
            state.read_bytes(),
            state.stat().st_mode & 0o777,
        )

    def _assert_raced_release_left_in_place(
        self,
        sha: str,
        source_name: str,
        expected_content: str,
    ) -> None:
        releases = self.home / "personal-sync" / "releases"
        self.assertEqual(
            (
                releases
                / sha
                / "personal_codex"
                / "skills"
                / source_name
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
            expected_content,
        )
        self.assertEqual(list(releases.glob(f".retained-{sha}-*")), [])

    def _install_private_for_link_race(self) -> Path:
        private_release = self.root / "private-link-race-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        return self.home / "skills" / "private"

    def test_overlay_only_install_rejects_base_sha_mismatch_before_planning(
        self,
    ) -> None:
        private_release = self.root / "private-pinned-to-b"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
            base_release_sha=SHA_B,
        )

        with (
            mock.patch.object(MODULE, "_plan_reconciliation") as plan,
            mock.patch.object(MODULE, "_stage_pending_link_batch") as stage,
            self.assertRaisesRegex(
                MODULE.SyncError,
                f"overlay private requires public release {SHA_B}.*{SHA_A}.*"
                "install-private",
            ),
        ):
            install_quietly(private_release, self.home, "c" * 40)

        plan.assert_not_called()
        stage.assert_not_called()
        self.assertFalse(
            os.path.lexists(MODULE._current_link(self.home, "private"))
        )

    def test_overlay_base_sha_validation_checks_all_overlays_and_missing_public(
        self,
    ) -> None:
        public_manifest = MODULE.ManifestData(
            owner=MODULE.PUBLIC_OWNER,
            entries=[],
            removed_links=[],
        )
        matching_overlay = MODULE.ManifestData(
            owner="matching",
            entries=[],
            removed_links=[],
            base_release_sha=SHA_A,
        )
        mismatched_overlay = MODULE.ManifestData(
            owner="mismatched",
            entries=[],
            removed_links=[],
            base_release_sha=SHA_B,
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            f"overlay mismatched requires public release {SHA_B}.*{SHA_A}",
        ):
            MODULE._validate_planned_overlay_base_release_shas(
                {
                    MODULE.PUBLIC_OWNER: public_manifest,
                    "matching": matching_overlay,
                    "mismatched": mismatched_overlay,
                },
                SHA_A,
            )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            f"overlay matching requires public release {SHA_A}.*no public release",
        ):
            MODULE._validate_planned_overlay_base_release_shas(
                {"matching": matching_overlay},
                None,
            )

    def test_public_upgrade_rejects_retained_overlay_base_sha_mismatch_before_planning(
        self,
    ) -> None:
        private_release = self.root / "private-pinned-to-a"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
            base_release_sha=SHA_A,
        )
        install_quietly(private_release, self.home, "c" * 40)
        public_current = MODULE._current_link(self.home, MODULE.PUBLIC_OWNER)
        private_current = MODULE._current_link(self.home, "private")
        state_path = MODULE._state_path(self.home)
        before = (
            os.readlink(public_current),
            os.readlink(private_current),
            state_path.read_bytes(),
        )

        with (
            mock.patch.object(MODULE, "_plan_reconciliation") as plan,
            mock.patch.object(MODULE, "_stage_pending_link_batch") as stage,
            self.assertRaisesRegex(
                MODULE.SyncError,
                f"overlay private requires public release {SHA_A}.*{SHA_B}",
            ),
        ):
            install_quietly(self.release_b, self.home, SHA_B)

        plan.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(
            (
                os.readlink(public_current),
                os.readlink(private_current),
                state_path.read_bytes(),
            ),
            before,
        )
        self.assertFalse(
            (self.home / "personal-sync" / "releases" / SHA_B).exists()
        )

    def test_public_rollback_rejects_retained_overlay_base_sha_mismatch_before_planning(
        self,
    ) -> None:
        install_quietly(self.release_b, self.home, SHA_B)
        private_release = self.root / "private-pinned-to-b"
        private_sha = "c" * 40
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
            base_release_sha=SHA_B,
        )
        install_quietly(private_release, self.home, private_sha)
        public_current = MODULE._current_link(self.home, MODULE.PUBLIC_OWNER)
        private_current = MODULE._current_link(self.home, "private")
        state_path = MODULE._state_path(self.home)
        before = (
            os.readlink(public_current),
            os.readlink(private_current),
            state_path.read_bytes(),
        )

        with (
            mock.patch.object(MODULE, "_plan_reconciliation") as plan,
            mock.patch.object(MODULE, "_stage_pending_link_batch") as stage,
            self.assertRaisesRegex(
                MODULE.SyncError,
                f"overlay private requires public release {SHA_B}.*{SHA_A}",
            ),
        ):
            MODULE.rollback(self.home, SHA_A, MODULE.PUBLIC_OWNER)

        plan.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(
            (
                os.readlink(public_current),
                os.readlink(private_current),
                state_path.read_bytes(),
            ),
            before,
        )

    def test_paired_public_and_pinned_overlay_install_uses_planned_public_sha(
        self,
    ) -> None:
        private_release = self.root / "private-paired-with-b"
        private_sha = "c" * 40
        private_manifest = write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
            base_release_sha=SHA_B,
        )
        public_manifest = MODULE.load_manifest_data(self.release_b)

        with (
            MODULE.installation_lock(self.home),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE._install_release_set_unlocked(
                self.home,
                [
                    (self.release_b, SHA_B, public_manifest),
                    (private_release, private_sha, private_manifest),
                ],
                dry_run=False,
                allow_cross_owner=True,
            )

        self.assertEqual(
            os.readlink(MODULE._current_link(self.home, MODULE.PUBLIC_OWNER)),
            f"releases/{SHA_B}",
        )
        self.assertEqual(
            os.readlink(MODULE._current_link(self.home, "private")),
            f"releases/{private_sha}",
        )
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_B, "private": private_sha},
        )

    def test_paired_install_rejects_overlay_pin_mismatch_before_planning(
        self,
    ) -> None:
        private_release = self.root / "private-paired-with-wrong-public"
        private_sha = "c" * 40
        private_manifest = write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
            base_release_sha=SHA_A,
        )
        public_manifest = MODULE.load_manifest_data(self.release_b)
        public_current = MODULE._current_link(self.home, MODULE.PUBLIC_OWNER)
        state_path = MODULE._state_path(self.home)
        before = (os.readlink(public_current), state_path.read_bytes())

        with (
            mock.patch.object(MODULE, "_plan_reconciliation") as plan,
            mock.patch.object(MODULE, "_stage_pending_link_batch") as stage,
            MODULE.installation_lock(self.home),
            self.assertRaisesRegex(
                MODULE.SyncError,
                f"overlay private requires public release {SHA_A}.*{SHA_B}",
            ),
        ):
            MODULE._install_release_set_unlocked(
                self.home,
                [
                    (self.release_b, SHA_B, public_manifest),
                    (private_release, private_sha, private_manifest),
                ],
                dry_run=False,
                allow_cross_owner=True,
            )

        plan.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(
            (os.readlink(public_current), state_path.read_bytes()),
            before,
        )
        self.assertFalse(
            os.path.lexists(MODULE._current_link(self.home, "private"))
        )
        self.assertFalse(
            (self.home / "personal-sync" / "releases" / SHA_B).exists()
        )
        self.assertFalse(
            (
                self.home
                / "personal-sync"
                / "overlays"
                / "private"
                / "releases"
                / private_sha
            ).exists()
        )

    def test_public_upgrade_allows_retained_unpinned_overlay(self) -> None:
        private_release = self.root / "private-unpinned"
        private_sha = "c" * 40
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, private_sha)

        install_quietly(self.release_b, self.home, SHA_B)

        self.assertEqual(
            os.readlink(MODULE._current_link(self.home, MODULE.PUBLIC_OWNER)),
            f"releases/{SHA_B}",
        )
        self.assertEqual(
            os.readlink(MODULE._current_link(self.home, "private")),
            f"releases/{private_sha}",
        )
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_B, "private": private_sha},
        )

    def test_install_rejects_same_target_link_inode_replacement(self) -> None:
        target = self.home / "skills" / "example"
        previous_target = os.readlink(target)
        real_apply = MODULE._apply_reconcile_actions
        racer_identity: tuple[int, int] | None = None

        def race_link_action(
            home: Path,
            actions: list[MODULE.ReconcileAction],
            *,
            dry_run: bool,
            required_replacements: dict[Path, list[MODULE.LinkEntry]] | None = None,
            **kwargs: object,
        ) -> MODULE.ReconcileTransaction | None:
            nonlocal racer_identity
            if racer_identity is None and any(action.target == target for action in actions):
                target.unlink()
                target.symlink_to(previous_target, target_is_directory=True)
                racer_identity = (target.lstat().st_dev, target.lstat().st_ino)
            return real_apply(
                home,
                actions,
                dry_run=dry_run,
                required_replacements=required_replacements,
                **kwargs,
            )

        with mock.patch.object(
            MODULE,
            "_apply_reconcile_actions",
            side_effect=race_link_action,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertEqual(os.readlink(target), previous_target)
        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            racer_identity,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_install_rejects_link_parent_replacement_after_planning(self) -> None:
        skills = self.home / "skills"
        target = skills / "example"
        previous_target = os.readlink(target)
        displaced_skills = self.home / "skills-displaced-during-install"
        real_apply = MODULE._apply_reconcile_actions
        raced = False

        def race_link_parent(
            home: Path,
            actions: list[MODULE.ReconcileAction],
            *,
            dry_run: bool,
            required_replacements: dict[Path, list[MODULE.LinkEntry]] | None = None,
            **kwargs: object,
        ) -> MODULE.ReconcileTransaction | None:
            nonlocal raced
            if not raced and any(action.target == target for action in actions):
                raced = True
                skills.rename(displaced_skills)
                skills.mkdir()
            return real_apply(
                home,
                actions,
                dry_run=dry_run,
                required_replacements=required_replacements,
                **kwargs,
            )

        with mock.patch.object(
            MODULE,
            "_apply_reconcile_actions",
            side_effect=race_link_parent,
        ):
            with self.assertRaises(MODULE.SyncError):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(raced)
        self.assertFalse(os.path.lexists(target))
        displaced_target = displaced_skills / "example"
        self.assertTrue(displaced_target.is_symlink())
        self.assertEqual(os.readlink(displaced_target), previous_target)

    def test_uninstall_rejects_same_target_link_inode_replacement(self) -> None:
        target = self._install_private_for_link_race()
        previous_target = os.readlink(target)
        real_apply = MODULE._apply_reconcile_actions
        racer_identity: tuple[int, int] | None = None

        def race_link_action(
            home: Path,
            actions: list[MODULE.ReconcileAction],
            *,
            dry_run: bool,
            required_replacements: dict[Path, list[MODULE.LinkEntry]] | None = None,
            **kwargs: object,
        ) -> MODULE.ReconcileTransaction | None:
            nonlocal racer_identity
            if racer_identity is None and any(action.target == target for action in actions):
                target.unlink()
                target.symlink_to(previous_target, target_is_directory=True)
                racer_identity = (target.lstat().st_dev, target.lstat().st_ino)
            return real_apply(
                home,
                actions,
                dry_run=dry_run,
                required_replacements=required_replacements,
                **kwargs,
            )

        with mock.patch.object(
            MODULE,
            "_apply_reconcile_actions",
            side_effect=race_link_action,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(os.readlink(target), previous_target)
        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            racer_identity,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_uninstall_rejects_link_parent_replacement_after_planning(self) -> None:
        target = self._install_private_for_link_race()
        previous_target = os.readlink(target)
        skills = target.parent
        displaced_skills = self.home / "skills-displaced-during-uninstall"
        real_apply = MODULE._apply_reconcile_actions
        raced = False

        def race_link_parent(
            home: Path,
            actions: list[MODULE.ReconcileAction],
            *,
            dry_run: bool,
            required_replacements: dict[Path, list[MODULE.LinkEntry]] | None = None,
            **kwargs: object,
        ) -> MODULE.ReconcileTransaction | None:
            nonlocal raced
            if not raced and any(action.target == target for action in actions):
                raced = True
                skills.rename(displaced_skills)
                skills.mkdir()
            return real_apply(
                home,
                actions,
                dry_run=dry_run,
                required_replacements=required_replacements,
                **kwargs,
            )

        with mock.patch.object(
            MODULE,
            "_apply_reconcile_actions",
            side_effect=race_link_parent,
        ):
            with self.assertRaises(MODULE.SyncError):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(raced)
        self.assertFalse(os.path.lexists(target))
        displaced_target = displaced_skills / "private"
        self.assertTrue(displaced_target.is_symlink())
        self.assertEqual(os.readlink(displaced_target), previous_target)

    def test_release_binding_rechecks_canonical_inode_after_current_snapshot(
        self,
    ) -> None:
        releases = self.home / "personal-sync" / "releases"
        canonical = releases / SHA_A
        displaced = releases / ".displaced-after-current-snapshot"
        replacement = self.root / "same-content-replacement"
        write_skill_release(replacement, source_name="old")
        expectation = MODULE._installed_release_identity_and_directory_identity(
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
        )
        binding = MODULE._open_install_release_binding(
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
            expectation,
        )
        real_identity = MODULE._release_tree_identity_from_directory_fd
        injected = False

        def scan_then_swap_canonical(
            root_fd: int,
            display_root: Path,
            *,
            require_sanitized_modes: bool = False,
        ) -> MODULE.ReleaseTreeIdentity:
            nonlocal injected
            identity = real_identity(
                root_fd,
                display_root,
                require_sanitized_modes=require_sanitized_modes,
            )
            if not injected:
                injected = True
                canonical.rename(displaced)
                replacement.rename(canonical)
            return identity

        try:
            with mock.patch.object(
                MODULE,
                "_release_tree_identity_from_directory_fd",
                side_effect=scan_then_swap_canonical,
            ):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "release tree changed during canonical recheck",
                ):
                    MODULE._verify_install_release_binding(
                        self.home,
                        binding,
                        phase="during canonical recheck",
                        verify_current=True,
                    )
        finally:
            MODULE._close_install_release_bindings([binding])

        self.assertTrue(injected)
        self.assertTrue(displaced.is_dir())
        self._assert_raced_release_left_in_place(SHA_A, "old", "# Example\n")

    def test_release_binding_detects_same_target_current_inode_swap(self) -> None:
        current = self.home / "personal-sync" / "current"
        original_metadata = current.lstat()
        expectation = MODULE._installed_release_identity_and_directory_identity(
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
        )
        binding = MODULE._open_install_release_binding(
            self.home,
            MODULE.PUBLIC_OWNER,
            SHA_A,
            expectation,
        )
        real_identity = MODULE._release_tree_identity_from_directory_fd
        injected = False

        def scan_then_replace_current(
            root_fd: int,
            display_root: Path,
            *,
            require_sanitized_modes: bool = False,
        ) -> MODULE.ReleaseTreeIdentity:
            nonlocal injected
            identity = real_identity(
                root_fd,
                display_root,
                require_sanitized_modes=require_sanitized_modes,
            )
            if not injected:
                injected = True
                current.unlink()
                current.symlink_to(f"releases/{SHA_A}", target_is_directory=True)
            return identity

        try:
            with mock.patch.object(
                MODULE,
                "_release_tree_identity_from_directory_fd",
                side_effect=scan_then_replace_current,
            ):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "release tree changed during current recheck",
                ):
                    MODULE._verify_install_release_binding(
                        self.home,
                        binding,
                        phase="during current recheck",
                        verify_current=True,
                    )
        finally:
            MODULE._close_install_release_bindings([binding])

        current_metadata = current.lstat()
        self.assertTrue(injected)
        self.assertEqual(os.readlink(current), f"releases/{SHA_A}")
        self.assertNotEqual(
            (current_metadata.st_dev, current_metadata.st_ino),
            (original_metadata.st_dev, original_metadata.st_ino),
        )

    def test_descriptor_bound_link_checks_reject_parent_swap(self) -> None:
        manifest = MODULE._current_manifest_data(self.home, MODULE.PUBLIC_OWNER)
        entry = manifest.entries[0]
        target = MODULE._entry_target_path(self.home, entry)
        desired = MODULE._desired_link_target(self.home, entry)
        operations = {
            "replacement": lambda: MODULE._verify_required_replacement_targets(
                self.home,
                [entry],
            ),
            "desired": lambda: MODULE._verify_desired_entries(
                self.home,
                [entry],
            ),
            "refresh": lambda: MODULE._refresh_managed_state_from_current(
                self.home,
                MODULE._load_managed_state(self.home),
                bootstrap_history=False,
            ),
        }

        for name, operation in operations.items():
            with self.subTest(operation=name):
                moved_parent = self.root / f"moved-skills-{name}"
                outside_parent = self.root / f"outside-skills-{name}"
                outside_parent.mkdir()
                outside_target = outside_parent / target.name
                outside_target.symlink_to(desired, target_is_directory=True)
                real_open_directory = MODULE._open_directory_beneath
                swapped = False

                def open_then_swap(root: Path, directory: Path) -> int:
                    nonlocal swapped
                    directory_fd = real_open_directory(root, directory)
                    if directory == target.parent and not swapped:
                        swapped = True
                        target.parent.rename(moved_parent)
                        target.parent.symlink_to(
                            outside_parent,
                            target_is_directory=True,
                        )
                    return directory_fd

                try:
                    with mock.patch.object(
                        MODULE,
                        "_open_directory_beneath",
                        side_effect=open_then_swap,
                    ):
                        with self.assertRaises(MODULE.SyncError):
                            operation()
                finally:
                    if target.parent.is_symlink():
                        target.parent.unlink()
                    if moved_parent.exists():
                        moved_parent.rename(target.parent)

                self.assertTrue(swapped)
                self.assertTrue(outside_target.is_symlink())
                self.assertEqual(os.readlink(outside_target), desired)

    def test_committed_state_rejects_mandatory_missing_and_drift(self) -> None:
        manifest = MODULE._current_manifest_data(self.home, MODULE.PUBLIC_OWNER)
        entry = manifest.entries[0]
        target = MODULE._entry_target_path(self.home, entry)
        desired = MODULE._desired_link_target(self.home, entry)
        owner_shas = {MODULE.PUBLIC_OWNER: SHA_A}

        target.unlink()
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "mandatory desired link missing",
        ):
            MODULE._committed_state(self.home, [entry], owner_shas)

        target.symlink_to("concurrent-drift", target_is_directory=True)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "mandatory desired link drifted",
        ):
            MODULE._committed_state(self.home, [entry], owner_shas)

        target.unlink()
        target.symlink_to(desired, target_is_directory=True)
        optional_entry = MODULE.LinkEntry(
            source=PurePosixPath("AGENTS.md"),
            target=PurePosixPath("AGENTS.md"),
            kind="file",
        )
        optional_target = self.home / "AGENTS.md"
        optional_target.symlink_to("local-agents")
        committed = MODULE._committed_state(
            self.home,
            [entry, optional_entry],
            owner_shas,
        )
        self.assertIn(entry.target, committed.links)
        self.assertNotIn(optional_entry.target, committed.links)

    def test_existing_release_with_different_valid_manifest_is_preserved(self) -> None:
        state_before = self._snapshot()
        installed = self.home / "personal-sync" / "releases" / SHA_A
        incoming = self.root / "same-sha-different-manifest"
        write_skill_release(incoming, source_name="different")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "existing release tree does not match incoming source",
        ):
            install_quietly(incoming, self.home, SHA_A)

        self.assertEqual(self._snapshot(), state_before)
        self.assertEqual(
            (
                installed
                / "personal_codex"
                / "skills"
                / "old"
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
            "# Example\n",
        )
        self.assertFalse(
            (installed / "personal_codex" / "skills" / "different").exists()
        )

    def test_existing_release_with_same_manifest_but_different_content_is_preserved(
        self,
    ) -> None:
        state_before = self._snapshot()
        installed_skill = (
            self.home
            / "personal-sync"
            / "releases"
            / SHA_A
            / "personal_codex"
            / "skills"
            / "old"
            / "SKILL.md"
        )
        incoming_skill = (
            self.release_a
            / "personal_codex"
            / "skills"
            / "old"
            / "SKILL.md"
        )
        incoming_skill.write_text("# Different content\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "existing release tree does not match incoming source",
        ):
            install_quietly(self.release_a, self.home, SHA_A)

        self.assertEqual(self._snapshot(), state_before)
        self.assertEqual(installed_skill.read_text(encoding="utf-8"), "# Example\n")

    def test_existing_release_reuses_same_content_across_safe_root_modes(self) -> None:
        state_before = self._snapshot()
        installed = self.home / "personal-sync" / "releases" / SHA_A
        installed_inode = installed.stat().st_ino
        installed.chmod(0o755)
        self.release_a.chmod(0o700)

        install_quietly(self.release_a, self.home, SHA_A)

        self.assertEqual(self._snapshot(), state_before)
        self.assertEqual(installed.stat().st_ino, installed_inode)
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)

    def test_source_mutation_after_capture_before_final_install_is_rejected(self) -> None:
        state_before = self._snapshot()
        source_skill = (
            self.release_b
            / "personal_codex"
            / "skills"
            / "new"
            / "SKILL.md"
        )
        real_install_set = MODULE._install_release_set_unlocked
        injected = False

        def install_then_mutate_source(
            home: Path,
            releases: list[tuple[object, ...]],
            *,
            dry_run: bool,
            allow_cross_owner: bool,
            preflight_only: bool = False,
        ) -> None:
            nonlocal injected
            if not dry_run and not injected:
                injected = True
                metadata = source_skill.stat()
                source_skill.write_bytes(b"# Changed\n")
                source_skill.chmod(stat.S_IMODE(metadata.st_mode))
                os.utime(
                    source_skill,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                )
            real_install_set(
                home,
                releases,
                dry_run=dry_run,
                allow_cross_owner=allow_cross_owner,
                preflight_only=preflight_only,
            )

        with mock.patch.object(
            MODULE,
            "_install_release_set_unlocked",
            side_effect=install_then_mutate_source,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release source changed after its captured identity",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self.assertFalse(
            (self.home / "personal-sync" / "releases" / SHA_B).exists()
        )
        self.assertEqual(source_skill.read_bytes(), b"# Changed\n")

    def test_unchanged_public_base_mutation_before_activation_is_rejected(
        self,
    ) -> None:
        state_before = self._snapshot()
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        public_skill = (
            self.home
            / "personal-sync"
            / "releases"
            / SHA_A
            / "personal_codex"
            / "skills"
            / "old"
            / "SKILL.md"
        )
        real_plan = MODULE._plan_current_switch_actions
        injected = False

        def plan_then_mutate(
            home: Path,
            releases: list[
                tuple[
                    Path,
                    str,
                    MODULE.ManifestData,
                    MODULE.ReleaseTreeExpectation,
                ]
            ],
        ) -> list[MODULE.ReconcileAction]:
            nonlocal injected
            actions = real_plan(home, releases)
            if not injected:
                injected = True
                public_skill.write_bytes(b"# Changed\n")
            return actions

        with mock.patch.object(
            MODULE,
            "_plan_current_switch_actions",
            side_effect=plan_then_mutate,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed before activation",
            ):
                install_quietly(private_release, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self.assertEqual(public_skill.read_bytes(), b"# Changed\n")
        self.assertFalse(
            os.path.lexists(
                self.home
                / "personal-sync"
                / "overlays"
                / "private"
                / "current"
            )
        )
        self.assertFalse(os.path.lexists(self.home / "skills" / "private"))

    def test_precommit_unchanged_private_mutation_rolls_back(self) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_target = self.home / "skills" / "private"
        state_before = (
            self._snapshot(),
            os.readlink(private_current),
            os.readlink(private_target),
        )
        private_skill = (
            self.home
            / "personal-sync"
            / "overlays"
            / "private"
            / "releases"
            / SHA_B
            / "personal_codex"
            / "skills"
            / "private"
            / "SKILL.md"
        )
        public_next = self.root / "public-next"
        write_skill_release(public_next, source_name="next")
        sha_c = "c" * 40
        real_verify = MODULE._verify_install_release_identities
        injected = False

        def mutate_then_verify_private(
            home: Path,
            bindings: list[MODULE.InstallReleaseBinding],
            *,
            phase: str,
            verify_current: bool,
        ) -> None:
            nonlocal injected
            if not injected and phase == "during final managed-state validation":
                injected = True
                metadata = private_skill.stat()
                private_skill.write_bytes(b"# Changed\n")
                private_skill.chmod(stat.S_IMODE(metadata.st_mode))
                os.utime(
                    private_skill,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                )
            real_verify(
                home,
                bindings,
                phase=phase,
                verify_current=verify_current,
            )

        with mock.patch.object(
            MODULE,
            "_verify_install_release_identities",
            side_effect=mutate_then_verify_private,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed during final managed-state validation",
            ):
                install_quietly(public_next, self.home, sha_c)

        state_after = (
            self._snapshot(),
            os.readlink(private_current),
            os.readlink(private_target),
        )
        self.assertTrue(injected)
        self.assertEqual(state_after, state_before)
        self.assertEqual(private_skill.read_bytes(), b"# Changed\n")

    def test_new_release_mutation_before_activation_is_retained(self) -> None:
        state_before = self._snapshot()
        installed_skill = (
            self.home
            / "personal-sync"
            / "releases"
            / SHA_B
            / "personal_codex"
            / "skills"
            / "new"
            / "SKILL.md"
        )
        real_stage = MODULE._stage_release_tree_for_install
        injected = False

        def stage_then_mutate(
            source_root: Path,
            home: Path,
            sha: str,
            manifest: MODULE.ManifestData,
            source_expectation: MODULE.ReleaseTreeExpectation,
        ) -> MODULE.InstallReleaseBinding:
            nonlocal injected
            identity = real_stage(
                source_root,
                home,
                sha,
                manifest,
                source_expectation,
            )
            if not injected and sha == SHA_B:
                injected = True
                installed_skill.write_text(
                    "# Raced before activation\n",
                    encoding="utf-8",
                )
            return identity

        with mock.patch.object(
            MODULE,
            "_stage_release_tree_for_install",
            side_effect=stage_then_mutate,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed before activation",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self._assert_raced_release_left_in_place(
            SHA_B,
            "new",
            "# Raced before activation\n",
        )

    def test_new_release_same_content_inode_swap_before_activation_is_rejected(
        self,
    ) -> None:
        state_before = self._snapshot()
        releases = self.home / "personal-sync" / "releases"
        displaced = releases / ".displaced-new-release"
        real_stage = MODULE._stage_release_tree_for_install
        observed_bindings: list[MODULE.InstallReleaseBinding] = []

        def stage_then_swap(
            source_root: Path,
            home: Path,
            sha: str,
            manifest: MODULE.ManifestData,
            source_expectation: MODULE.ReleaseTreeExpectation,
        ) -> MODULE.InstallReleaseBinding:
            binding = real_stage(
                source_root,
                home,
                sha,
                manifest,
                source_expectation,
            )
            observed_bindings.append(binding)
            (releases / sha).rename(displaced)
            write_skill_release(releases / sha, source_name="new")
            return binding

        with mock.patch.object(
            MODULE,
            "_stage_release_tree_for_install",
            side_effect=stage_then_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed before activation",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertEqual(self._snapshot(), state_before)
        self.assertEqual(len(observed_bindings), 1)
        self.assertEqual(observed_bindings[0].release_fd, -1)
        self.assertEqual(observed_bindings[0].releases_fd, -1)
        self.assertTrue(displaced.is_dir())
        self._assert_raced_release_left_in_place(SHA_B, "new", "# Example\n")

    def test_new_release_mutation_before_state_publication_rolls_back(self) -> None:
        state_before = self._snapshot()
        installed_skill = (
            self.home
            / "personal-sync"
            / "releases"
            / SHA_B
            / "personal_codex"
            / "skills"
            / "new"
            / "SKILL.md"
        )
        real_verify = MODULE._verify_install_release_identities
        injected = False

        def mutate_then_verify(
            home: Path,
            bindings: list[MODULE.InstallReleaseBinding],
            *,
            phase: str,
            verify_current: bool,
        ) -> None:
            nonlocal injected
            if not injected and phase == "during final managed-state validation":
                injected = True
                metadata = installed_skill.stat()
                installed_skill.write_bytes(b"# Changed\n")
                installed_skill.chmod(stat.S_IMODE(metadata.st_mode))
                os.utime(
                    installed_skill,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                )
            real_verify(
                home,
                bindings,
                phase=phase,
                verify_current=verify_current,
            )

        with mock.patch.object(
            MODULE,
            "_verify_install_release_identities",
            side_effect=mutate_then_verify,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed during final managed-state validation",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self._assert_raced_release_left_in_place(
            SHA_B,
            "new",
            "# Changed\n",
        )

    def test_same_sha_mutation_before_activation_is_retained(self) -> None:
        state_before = self._snapshot()
        installed_skill = (
            self.home
            / "personal-sync"
            / "releases"
            / SHA_A
            / "personal_codex"
            / "skills"
            / "old"
            / "SKILL.md"
        )
        real_stage = MODULE._stage_release_tree_for_install
        injected = False

        def stage_then_mutate(
            source_root: Path,
            home: Path,
            sha: str,
            manifest: MODULE.ManifestData,
            source_expectation: MODULE.ReleaseTreeExpectation,
        ) -> MODULE.InstallReleaseBinding:
            nonlocal injected
            identity = real_stage(
                source_root,
                home,
                sha,
                manifest,
                source_expectation,
            )
            if not injected and sha == SHA_A:
                injected = True
                installed_skill.write_text(
                    "# Same-SHA race before activation\n",
                    encoding="utf-8",
                )
            return identity

        with mock.patch.object(
            MODULE,
            "_stage_release_tree_for_install",
            side_effect=stage_then_mutate,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed before activation",
            ):
                install_quietly(self.release_a, self.home, SHA_A)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self._assert_raced_release_left_in_place(
            SHA_A,
            "old",
            "# Same-SHA race before activation\n",
        )

    def test_same_sha_same_content_inode_swap_before_activation_is_rejected(
        self,
    ) -> None:
        state_before = self._snapshot()
        releases = self.home / "personal-sync" / "releases"
        displaced = releases / ".displaced-same-sha-release"
        real_stage = MODULE._stage_release_tree_for_install

        def stage_then_swap(
            source_root: Path,
            home: Path,
            sha: str,
            manifest: MODULE.ManifestData,
            source_expectation: MODULE.ReleaseTreeExpectation,
        ) -> MODULE.InstallReleaseBinding:
            binding = real_stage(
                source_root,
                home,
                sha,
                manifest,
                source_expectation,
            )
            (releases / sha).rename(displaced)
            write_skill_release(releases / sha, source_name="old")
            return binding

        with mock.patch.object(
            MODULE,
            "_stage_release_tree_for_install",
            side_effect=stage_then_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed before activation",
            ):
                install_quietly(self.release_a, self.home, SHA_A)

        self.assertEqual(self._snapshot(), state_before)
        self.assertTrue(displaced.is_dir())
        self._assert_raced_release_left_in_place(SHA_A, "old", "# Example\n")

    def test_same_sha_mutation_before_state_publication_rolls_back(self) -> None:
        state_before = self._snapshot()
        installed_skill = (
            self.home
            / "personal-sync"
            / "releases"
            / SHA_A
            / "personal_codex"
            / "skills"
            / "old"
            / "SKILL.md"
        )
        real_verify = MODULE._verify_install_release_identities
        injected = False

        def mutate_then_verify(
            home: Path,
            bindings: list[MODULE.InstallReleaseBinding],
            *,
            phase: str,
            verify_current: bool,
        ) -> None:
            nonlocal injected
            if not injected and phase == "during final managed-state validation":
                injected = True
                metadata = installed_skill.stat()
                installed_skill.write_bytes(b"# Changed\n")
                installed_skill.chmod(stat.S_IMODE(metadata.st_mode))
                os.utime(
                    installed_skill,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                )
            real_verify(
                home,
                bindings,
                phase=phase,
                verify_current=verify_current,
            )

        with mock.patch.object(
            MODULE,
            "_verify_install_release_identities",
            side_effect=mutate_then_verify,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed during final managed-state validation",
            ):
                install_quietly(self.release_a, self.home, SHA_A)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self._assert_raced_release_left_in_place(
            SHA_A,
            "old",
            "# Changed\n",
        )

    def test_install_owner_shas_ignore_manifest_compatible_current_aba(self) -> None:
        sha_y = "c" * 40
        release_y = self.home / "personal-sync" / "releases" / sha_y
        manifest_y = write_skill_release(release_y, source_name="new")
        self.assertEqual(manifest_y, MODULE.load_manifest_data(self.release_b))
        current = self.home / "personal-sync" / "current"
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        real_current_sha = MODULE._current_sha
        real_verify_desired = MODULE._verify_desired_entries
        mapping_phase = False
        injected = False

        def verify_then_enter_mapping_phase(
            home: Path,
            desired_entries: list[MODULE.LinkEntry],
        ) -> None:
            nonlocal mapping_phase
            real_verify_desired(home, desired_entries)
            mapping_phase = True

        def current_sha_with_aba(
            home: Path,
            owner: str = MODULE.PUBLIC_OWNER,
        ) -> str | None:
            nonlocal injected
            if (
                mapping_phase
                and not injected
                and owner == MODULE.PUBLIC_OWNER
                and os.readlink(current) == f"releases/{SHA_B}"
            ):
                injected = True
                current.unlink()
                current.symlink_to(f"releases/{sha_y}", target_is_directory=True)
                try:
                    return real_current_sha(home, owner)
                finally:
                    current.unlink()
                    current.symlink_to(
                        f"releases/{SHA_B}",
                        target_is_directory=True,
                    )
            return real_current_sha(home, owner)

        with (
            mock.patch.object(
                MODULE,
                "_verify_desired_entries",
                side_effect=verify_then_enter_mapping_phase,
            ),
            mock.patch.object(
                MODULE,
                "_current_sha",
                side_effect=current_sha_with_aba,
            ),
        ):
            install_quietly(self.release_b, self.home, SHA_B)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(injected)
        self.assertEqual(os.readlink(current), f"releases/{SHA_B}")
        self.assertEqual(state["owners"], {MODULE.PUBLIC_OWNER: SHA_B})
        self.assertEqual(
            {link["release_sha"] for link in state["links"]},
            {SHA_B},
        )

    def test_install_preserves_ledger_mutated_during_release_scan(self) -> None:
        state_before = self._snapshot()
        current = self.home / "personal-sync" / "current"
        target = self.home / "skills" / "example"
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        real_identity = MODULE._release_tree_identity_from_directory_fd
        injected = False

        def scan_then_mutate_ledger(
            root_fd: int,
            display_root: Path,
            *,
            require_sanitized_modes: bool = False,
        ) -> MODULE.ReleaseTreeIdentity:
            nonlocal injected
            identity = real_identity(
                root_fd,
                display_root,
                require_sanitized_modes=require_sanitized_modes,
            )
            if (
                not injected
                and current.is_symlink()
                and os.readlink(current) == f"releases/{SHA_B}"
            ):
                injected = True
                state_path.write_bytes(b"concurrent ledger\n")
            return identity

        with mock.patch.object(
            MODULE,
            "_release_tree_identity_from_directory_fd",
            side_effect=scan_then_mutate_ledger,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "installation failed.*rollback was incomplete",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(os.readlink(current), state_before[0])
        self.assertEqual(os.readlink(target), state_before[1])
        self.assertEqual(state_path.read_bytes(), b"concurrent ledger\n")
        quarantine = self.home / "personal-sync" / "quarantine"
        raced_ledgers = list(quarantine.glob("*/state/rollback-current-*"))
        self.assertEqual(raced_ledgers, [])
        original_ledgers = list(quarantine.glob("*/state/managed-links.json"))
        self.assertEqual(original_ledgers, [])

    def test_install_preserves_same_target_link_replaced_during_release_scan(
        self,
    ) -> None:
        state_before = self._snapshot()
        current = self.home / "personal-sync" / "current"
        target = self.home / "skills" / "example"
        displaced_target = self.root / "transaction-created-link"
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        real_identity = MODULE._release_tree_identity_from_directory_fd
        injected = False
        raced_snapshot: tuple[int, int, str] | None = None

        def scan_then_replace_link_inode(
            root_fd: int,
            display_root: Path,
            *,
            require_sanitized_modes: bool = False,
        ) -> MODULE.ReleaseTreeIdentity:
            nonlocal injected, raced_snapshot
            identity = real_identity(
                root_fd,
                display_root,
                require_sanitized_modes=require_sanitized_modes,
            )
            if (
                not injected
                and current.is_symlink()
                and os.readlink(current) == f"releases/{SHA_B}"
            ):
                injected = True
                same_target = os.readlink(target)
                target.rename(displaced_target)
                target.symlink_to(same_target, target_is_directory=True)
                metadata = target.lstat()
                raced_snapshot = (
                    metadata.st_dev,
                    metadata.st_ino,
                    same_target,
                )
            return identity

        with mock.patch.object(
            MODULE,
            "_release_tree_identity_from_directory_fd",
            side_effect=scan_then_replace_link_inode,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "installation failed.*rollback was incomplete",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        assert raced_snapshot is not None
        target_metadata = target.lstat()
        self.assertEqual(os.readlink(current), state_before[0])
        self.assertEqual(state_path.read_bytes(), state_before[2])
        self.assertEqual(
            (target_metadata.st_dev, target_metadata.st_ino, os.readlink(target)),
            raced_snapshot,
        )

    def test_install_rejects_same_target_link_swap_during_planning(self) -> None:
        state_before = self._snapshot()
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        public_target = self.home / "skills" / "example"
        displaced_public_target = self.root / "planning-original-link"
        private_target = self.home / "skills" / "private"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        real_plan = MODULE._plan_reconciliation
        plan_calls = 0
        injected = False
        raced_snapshot: tuple[int, int, str] | None = None

        def plan_then_replace_link_inode(
            home: Path,
            desired_entries: list[MODULE.LinkEntry],
            previous_entries: list[MODULE.LinkEntry],
            removed_links: list[MODULE.RemovedLink],
            state: MODULE.ManagedState,
            *,
            allow_cross_owner: bool,
        ) -> list[MODULE.ReconcileAction]:
            nonlocal injected, plan_calls, raced_snapshot
            actions = real_plan(
                home,
                desired_entries,
                previous_entries,
                removed_links,
                state,
                allow_cross_owner=allow_cross_owner,
            )
            plan_calls += 1
            if not injected and plan_calls == 2:
                injected = True
                same_target = os.readlink(public_target)
                public_target.rename(displaced_public_target)
                public_target.symlink_to(same_target, target_is_directory=True)
                metadata = public_target.lstat()
                raced_snapshot = (
                    metadata.st_dev,
                    metadata.st_ino,
                    same_target,
                )
            return actions

        with mock.patch.object(
            MODULE,
            "_plan_reconciliation",
            side_effect=plan_then_replace_link_inode,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed link changed before publication",
            ):
                install_quietly(private_release, self.home, SHA_B)

        self.assertTrue(injected)
        assert raced_snapshot is not None
        target_metadata = public_target.lstat()
        self.assertEqual(state_path.read_bytes(), state_before[2])
        self.assertEqual(
            (
                target_metadata.st_dev,
                target_metadata.st_ino,
                os.readlink(public_target),
            ),
            raced_snapshot,
        )
        self.assertFalse(os.path.lexists(private_target))
        self.assertFalse(os.path.lexists(private_current))

    def test_overlay_uninstall_rejects_regular_file_and_preserves_ledger(
        self,
    ) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_target = self.home / "skills" / "private"
        private_target.unlink()
        private_target.write_text("local regular file\n", encoding="utf-8")
        regular_snapshot = (
            private_target.stat().st_dev,
            private_target.stat().st_ino,
            private_target.stat().st_mode,
            private_target.read_bytes(),
        )
        state_path = MODULE._state_path(self.home)
        state_before = state_path.read_bytes()
        current_before = os.readlink(private_current)
        quarantine = MODULE._personal_sync_root(self.home) / "quarantine"
        quarantine_before = tuple(sorted(path.name for path in quarantine.iterdir()))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed state/link target mismatch",
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(os.readlink(private_current), current_before)
        self.assertTrue(private_target.is_file())
        self.assertFalse(private_target.is_symlink())
        self.assertEqual(
            (
                private_target.stat().st_dev,
                private_target.stat().st_ino,
                private_target.stat().st_mode,
                private_target.read_bytes(),
            ),
            regular_snapshot,
        )
        self.assertEqual(state_path.read_bytes(), state_before)
        state = MODULE._load_managed_state(self.home)
        self.assertIn(PurePosixPath("skills/private"), state.links)
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )
        self.assertEqual(
            tuple(sorted(path.name for path in quarantine.iterdir())),
            quarantine_before,
        )

    def test_overlay_uninstall_bootstraps_without_managed_state_parent(
        self,
    ) -> None:
        private_release = self.root / "private-bootstrap-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        public_current = self.home / "personal-sync" / "current"
        public_target = self.home / "skills" / "example"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_target = self.home / "skills" / "private"
        public_current_before = MODULE._read_symlink_snapshot_beneath(
            self.home,
            public_current,
        )
        public_target_before = MODULE._read_symlink_snapshot_beneath(
            self.home,
            public_target,
        )
        state_path = MODULE._state_path(self.home)
        state_path.unlink()
        state_path.parent.rmdir()

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                public_current,
            ),
            public_current_before,
        )
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                public_target,
            ),
            public_target_before,
        )
        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(private_target))
        self.assertTrue(state_path.is_file())
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners, {MODULE.PUBLIC_OWNER: SHA_A})
        self.assertEqual(set(state.links), {PurePosixPath("skills/example")})
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )

    def test_overlay_uninstall_without_managed_state_rolls_back_before_commit(
        self,
    ) -> None:
        private_release = self.root / "private-bootstrap-rollback-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_current = MODULE._current_link(self.home, "private")
        private_target = self.home / "skills" / "private"
        private_current_before = MODULE._read_symlink_snapshot_beneath(
            self.home,
            private_current,
        )
        private_target_before = MODULE._read_symlink_snapshot_beneath(
            self.home,
            private_target,
        )
        state_path = MODULE._state_path(self.home)
        state_path.unlink()
        state_path.parent.rmdir()

        with mock.patch.object(
            MODULE,
            "_write_managed_state",
            side_effect=MODULE.SyncError("injected bootstrap pre-commit crash"),
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "injected bootstrap pre-commit crash",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(state_path.exists())
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_current,
            ).link_identity,
            private_current_before.link_identity,
        )
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_target,
            ).link_identity,
            private_target_before.link_identity,
        )
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )

    def test_overlay_uninstall_without_managed_state_retries_after_recovery(
        self,
    ) -> None:
        private_release = self.root / "private-bootstrap-recovery-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        public_target = self.home / "skills" / "example"
        public_link_target = os.readlink(public_target)
        public_target.unlink()
        private_current = MODULE._current_link(self.home, "private")
        private_target = self.home / "skills" / "private"
        state_path = MODULE._state_path(self.home)
        state_path.unlink()
        state_path.parent.rmdir()
        real_clear_pointer = MODULE._clear_pending_link_pointer

        def retain_precommit_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "before":
                raise MODULE.SyncError("injected crash retention")
            real_clear_pointer(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected bootstrap pre-commit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_precommit_pointer,
            ),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        pointer = MODULE._pending_link_pointer_path(self.home)
        self.assertTrue(pointer.is_file())
        pending_batch = MODULE._load_pending_link_batch(self.home)
        assert pending_batch is not None
        self.assertFalse(
            os.path.lexists(
                pending_batch.batch_root / pending_batch.commit_marker_path
            )
        )
        self.assertFalse(state_path.exists())
        self.assertFalse(os.path.lexists(public_target))
        self.assertTrue(os.path.lexists(private_current))
        self.assertTrue(os.path.lexists(private_target))

        public_target.symlink_to(public_link_target, target_is_directory=True)
        foreign_identity = (
            public_target.lstat().st_dev,
            public_target.lstat().st_ino,
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending before-state create absence changed",
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(pointer.is_file())
        self.assertEqual(
            (public_target.lstat().st_dev, public_target.lstat().st_ino),
            foreign_identity,
        )
        self.assertFalse(state_path.exists())
        self.assertTrue(os.path.lexists(private_current))
        self.assertTrue(os.path.lexists(private_target))
        public_target.unlink()

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(os.path.lexists(pointer))
        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(private_target))
        self.assertTrue(os.path.lexists(public_target))
        self.assertEqual(os.readlink(public_target), public_link_target)
        self.assertNotEqual(
            (public_target.lstat().st_dev, public_target.lstat().st_ino),
            foreign_identity,
        )
        self.assertEqual(MODULE._current_sha(self.home), SHA_A)
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners, {MODULE.PUBLIC_OWNER: SHA_A})
        self.assertEqual(set(state.links), {PurePosixPath("skills/example")})

    def test_overlay_uninstall_without_managed_state_finalizes_after_commit(
        self,
    ) -> None:
        private_release = self.root / "private-bootstrap-commit-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_current = MODULE._current_link(self.home, "private")
        private_target = self.home / "skills" / "private"
        state_path = MODULE._state_path(self.home)
        state_path.unlink()
        state_path.parent.rmdir()
        real_publish_marker = MODULE._publish_pending_commit_marker

        def publish_marker_then_fail(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> None:
            real_publish_marker(home, batch)
            raise MODULE.SyncError("injected bootstrap post-commit crash")

        with mock.patch.object(
            MODULE,
            "_publish_pending_commit_marker",
            side_effect=publish_marker_then_fail,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        pointer = MODULE._pending_link_pointer_path(self.home)
        self.assertTrue(pointer.is_file())
        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(private_target))

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(os.path.lexists(pointer))
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners, {MODULE.PUBLIC_OWNER: SHA_A})
        self.assertEqual(set(state.links), {PurePosixPath("skills/example")})

    def test_overlay_uninstall_retires_missing_ledger_link_and_parent(
        self,
    ) -> None:
        private_release = self.root / "private-retired-absence-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="retired/private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_target = self.home / "skills" / "retired" / "private"
        private_target.unlink()
        private_target.parent.rmdir()

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(private_target))
        self.assertFalse(private_target.parent.exists())
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners, {MODULE.PUBLIC_OWNER: SHA_A})
        self.assertNotIn(PurePosixPath("skills/retired/private"), state.links)
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )

    def test_retired_absence_rebinds_after_sibling_create_restores_parent(
        self,
    ) -> None:
        public_release = self.root / "public-sibling-release"
        write_skill_release(
            public_release,
            source_name="old",
            target_name="example",
        )
        public_source = public_release / "personal_codex" / "skills" / "public-new"
        public_source.mkdir(parents=True)
        (public_source / "SKILL.md").write_text("# Public new\n", encoding="utf-8")
        manifest_path = public_release / MODULE.MANIFEST_RELATIVE_PATH
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["links"].append(
            {
                "source": "personal_codex/skills/public-new",
                "target": "skills/retired/new",
                "kind": "skill",
                "owner": MODULE.PUBLIC_OWNER,
            }
        )
        manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        install_quietly(public_release, self.home, "c" * 40)

        private_release = self.root / "private-sibling-release"
        write_skill_release(
            private_release,
            source_name="private-old",
            target_name="retired/old",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        sibling_parent = self.home / "skills" / "retired"
        public_target = sibling_parent / "new"
        private_target = sibling_parent / "old"
        public_target.unlink()
        private_target.unlink()
        sibling_parent.rmdir()

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(public_target.is_symlink())
        self.assertEqual(
            (public_target / "SKILL.md").read_text(encoding="utf-8"),
            "# Public new\n",
        )
        self.assertFalse(os.path.lexists(private_target))
        state = MODULE._load_managed_state(self.home)
        self.assertIn(PurePosixPath("skills/retired/new"), state.links)
        self.assertNotIn(PurePosixPath("skills/retired/old"), state.links)
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )

    def test_overlay_uninstall_retired_absence_race_preserves_foreign_link(
        self,
    ) -> None:
        private_release = self.root / "private-retired-race-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_target = self.home / "skills" / "private"
        state_path = MODULE._state_path(self.home)
        state_before = state_path.read_bytes()
        current_before = MODULE._read_symlink_snapshot_beneath(
            self.home,
            private_current,
        )
        private_target.unlink()
        raced_snapshot: MODULE.SymlinkSnapshot | None = None

        def add_foreign_link_then_fail(
            home: Path,
            state: MODULE.ManagedState,
            transaction: MODULE.ManagedStateFileTransaction | None = None,
        ) -> None:
            nonlocal raced_snapshot
            private_target.symlink_to("../local-private", target_is_directory=True)
            raced_snapshot = MODULE._read_symlink_snapshot_beneath(
                home,
                private_target,
            )
            raise MODULE.SyncError("injected retired-absence race")

        with mock.patch.object(
            MODULE,
            "_write_managed_state",
            side_effect=add_foreign_link_then_fail,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        assert raced_snapshot is not None
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_target,
            ),
            raced_snapshot,
        )
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_current,
            ).link_identity,
            current_before.link_identity,
        )
        pointer = MODULE._pending_link_pointer_path(self.home)
        self.assertTrue(pointer.is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "retired absence found a foreign target",
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.uninstall_overlay(self.home, "private", dry_run=False)
        self.assertTrue(pointer.is_file())
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_target,
            ),
            raced_snapshot,
        )

    def test_committed_retired_absence_preserves_later_foreign_file(
        self,
    ) -> None:
        private_release = self.root / "private-retired-post-commit-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_target = self.home / "skills" / "private"
        private_target.unlink()
        real_publish_marker = MODULE._publish_pending_commit_marker

        def publish_marker_then_create_foreign_file(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> None:
            real_publish_marker(home, batch)
            private_target.write_text("foreign\n", encoding="utf-8")
            raise MODULE.SyncError("injected post-commit foreign file")

        with mock.patch.object(
            MODULE,
            "_publish_pending_commit_marker",
            side_effect=publish_marker_then_create_foreign_file,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        pointer = MODULE._pending_link_pointer_path(self.home)
        self.assertTrue(pointer.is_file())
        self.assertEqual(private_target.read_text(encoding="utf-8"), "foreign\n")

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(os.path.lexists(pointer))
        self.assertEqual(private_target.read_text(encoding="utf-8"), "foreign\n")

    def test_pending_parser_rejects_state_fields_on_retired_absence(
        self,
    ) -> None:
        private_release = self.root / "private-retired-parser-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_target = self.home / "skills" / "private"
        private_target.unlink()
        pointer = MODULE._pending_link_pointer_path(self.home)
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected pointer retention")
            real_clear(home, batch, phase=phase)

        with mock.patch.object(
            MODULE,
            "_clear_pending_link_pointer",
            side_effect=retain_committed_pointer,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        batch = MODULE._load_pending_link_batch(self.home)
        assert batch is not None
        retired_records = [
            record for record in batch.records if record.action == "retire-absent"
        ]
        self.assertEqual(len(retired_records), 1)
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        retired_payload = next(
            record
            for record in payload["records"]
            if record["action"] == "retire-absent"
        )
        retired_payload["owner"] = "private"
        pointer.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "retired target is not an exact state transition",
        ):
            MODULE._load_pending_link_batch(self.home)

    def test_installed_manifest_validation_rejects_symlinked_ancestor(self) -> None:
        installed = self.home / "personal-sync" / "releases" / SHA_A
        installed_personal = installed / "personal_codex"
        displaced_personal = installed / "personal_codex-original"
        outside_release = self.root / "outside-release"
        write_skill_release(outside_release, source_name="old")
        outside_personal = outside_release / "personal_codex"
        installed_personal.rename(displaced_personal)
        installed_personal.symlink_to(outside_personal, target_is_directory=True)

        with self.assertRaises(MODULE.SyncError):
            MODULE.current_release_entries(self.home)

        self.assertTrue(installed_personal.is_symlink())
        self.assertEqual(
            (outside_personal / "skills" / "old" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
            "# Example\n",
        )

    def test_non_manifest_staging_mutation_is_retained_after_publication(self) -> None:
        releases = self.home / "personal-sync" / "releases"
        state_before = self._snapshot()
        real_rename = MODULE._rename_noreplace_at
        injected = False

        def mutate_staging_then_publish(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal injected
            if (
                not injected
                and source_name.startswith(f".tmp-{SHA_B}-")
                and destination_name == SHA_B
            ):
                injected = True
                (
                    releases
                    / source_name
                    / "personal_codex"
                    / "skills"
                    / "new"
                    / "SKILL.md"
                ).write_text("# Concurrent mutation\n", encoding="utf-8")
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with mock.patch.object(
            MODULE,
            "_rename_noreplace_at",
            side_effect=mutate_staging_then_publish,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "published release tree differs",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self.assertFalse((releases / SHA_B).exists())
        retained = list(releases.glob(f".retained-{SHA_B}-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            (
                retained[0]
                / "personal_codex"
                / "skills"
                / "new"
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
            "# Concurrent mutation\n",
        )

    def test_release_parent_swap_during_copy_does_not_publish_elsewhere(self) -> None:
        releases = self.home / "personal-sync" / "releases"
        moved_releases = self.root / "moved-releases"
        outside = self.root / "outside"
        outside.mkdir()
        state_before = self._snapshot()
        real_copy = MODULE._copy_tree_from_directory_fd

        def copy_then_swap(
            source_fd: int,
            destination_fd: int,
            display_root: Path,
            relative_root: PurePosixPath,
            source_snapshots: dict[
                PurePosixPath,
                MODULE._ReleaseSourceSnapshot,
            ],
            source_members: dict[PurePosixPath, tuple[str, ...]],
        ) -> None:
            real_copy(
                source_fd,
                destination_fd,
                display_root,
                relative_root,
                source_snapshots,
                source_members,
            )
            if display_root == self.release_b and not relative_root.parts:
                releases.rename(moved_releases)
                releases.symlink_to(outside, target_is_directory=True)

        with mock.patch.object(
            MODULE,
            "_copy_tree_from_directory_fd",
            side_effect=copy_then_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release parent changed during copy",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertEqual(self._snapshot(), state_before)
        self.assertFalse((outside / SHA_B).exists())
        self.assertFalse((moved_releases / SHA_B).exists())
        staged = list(moved_releases.glob(f".retained-{SHA_B}-*"))
        self.assertEqual(len(staged), 1)
        self.assertTrue(
            (staged[0] / "personal_codex" / "skills" / "new" / "SKILL.md").is_file()
        )

    def test_replaced_staging_is_retained_without_canonical_release(self) -> None:
        releases = self.home / "personal-sync" / "releases"
        displaced_staging = releases / ".displaced-staging"
        state_before = self._snapshot()
        real_rename = MODULE._rename_noreplace_at
        injected = False

        def replace_staging_then_rename(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal injected
            if (
                not injected
                and source_name.startswith(f".tmp-{SHA_B}-")
                and destination_name == SHA_B
            ):
                injected = True
                (releases / source_name).rename(displaced_staging)
                replacement = releases / source_name
                replacement.mkdir()
                (replacement / "attacker-marker").write_text(
                    "replacement staging\n",
                    encoding="utf-8",
                )
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with mock.patch.object(
            MODULE,
            "_rename_noreplace_at",
            side_effect=replace_staging_then_rename,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "published release changed",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self.assertFalse((releases / SHA_B).exists())
        retained = list(releases.glob(f".retained-{SHA_B}-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            (retained[0] / "attacker-marker").read_text(encoding="utf-8"),
            "replacement staging\n",
        )
        self.assertTrue(
            (
                displaced_staging
                / "personal_codex"
                / "skills"
                / "new"
                / "SKILL.md"
            ).is_file()
        )

    def test_replaced_published_release_is_retained_without_canonical_name(
        self,
    ) -> None:
        releases = self.home / "personal-sync" / "releases"
        displaced_release = releases / ".displaced-published-release"
        state_before = self._snapshot()
        real_rename = MODULE._rename_noreplace_at
        injected = False

        def replace_after_rename(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal injected
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                not injected
                and source_name.startswith(f".tmp-{SHA_B}-")
                and destination_name == SHA_B
            ):
                injected = True
                (releases / SHA_B).rename(displaced_release)
                replacement = releases / SHA_B
                replacement.mkdir()
                (replacement / "attacker-marker").write_text(
                    "replacement release\n",
                    encoding="utf-8",
                )

        with mock.patch.object(
            MODULE,
            "_rename_noreplace_at",
            side_effect=replace_after_rename,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "published release changed",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), state_before)
        self.assertFalse((releases / SHA_B).exists())
        retained = list(releases.glob(f".retained-{SHA_B}-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            (retained[0] / "attacker-marker").read_text(encoding="utf-8"),
            "replacement release\n",
        )
        self.assertTrue(
            (
                displaced_release
                / "personal_codex"
                / "skills"
                / "new"
                / "SKILL.md"
            ).is_file()
        )

    def test_source_root_swap_after_open_aborts_without_copying_outside(self) -> None:
        releases = self.home / "personal-sync" / "releases"
        displaced_source = self.root / "displaced-release-b"
        outside_source = self.root / "outside-release"
        outside_skill = outside_source / "personal_codex" / "skills" / "new"
        outside_skill.mkdir(parents=True)
        (outside_skill / "SKILL.md").write_text("# Outside\n", encoding="utf-8")
        real_copy = MODULE._copy_tree_from_directory_fd
        injected = False

        def swap_source_then_copy(
            source_fd: int,
            destination_fd: int,
            display_root: Path,
            relative_root: PurePosixPath,
            source_snapshots: dict[
                PurePosixPath,
                MODULE._ReleaseSourceSnapshot,
            ],
            source_members: dict[PurePosixPath, tuple[str, ...]],
        ) -> None:
            nonlocal injected
            if not injected and display_root == self.release_b and not relative_root.parts:
                injected = True
                self.release_b.rename(displaced_source)
                self.release_b.symlink_to(outside_source, target_is_directory=True)
            real_copy(
                source_fd,
                destination_fd,
                display_root,
                relative_root,
                source_snapshots,
                source_members,
            )

        with mock.patch.object(
            MODULE,
            "_copy_tree_from_directory_fd",
            side_effect=swap_source_then_copy,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "release source changed"):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertFalse((releases / SHA_B).exists())
        retained = list(releases.glob(f".retained-{SHA_B}-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            (
                retained[0]
                / "personal_codex"
                / "skills"
                / "new"
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
            "# Example\n",
        )

    def test_source_child_swap_after_open_aborts_without_following_symlink(
        self,
    ) -> None:
        releases = self.home / "personal-sync" / "releases"
        source_child = self.release_b / "personal_codex"
        displaced_child = self.release_b / "personal_codex-original"
        outside_child = self.root / "outside-personal-codex"
        outside_skill = outside_child / "skills" / "new"
        outside_skill.mkdir(parents=True)
        (outside_skill / "SKILL.md").write_text("# Outside\n", encoding="utf-8")
        real_open = MODULE._open_source_directory_entry
        injected = False

        def open_then_swap(
            parent_fd: int,
            name: str,
            snapshot: MODULE._ReleaseSourceSnapshot,
            display_path: Path,
        ) -> int:
            nonlocal injected
            child_fd = real_open(parent_fd, name, snapshot, display_path)
            if not injected and display_path == source_child:
                injected = True
                source_child.rename(displaced_child)
                source_child.symlink_to(outside_child, target_is_directory=True)
            return child_fd

        with mock.patch.object(
            MODULE,
            "_open_source_directory_entry",
            side_effect=open_then_swap,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "release source changed"):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertFalse((releases / SHA_B).exists())
        retained = list(releases.glob(f".retained-{SHA_B}-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            (
                retained[0]
                / "personal_codex"
                / "skills"
                / "new"
                / "SKILL.md"
            ).read_text(encoding="utf-8"),
            "# Example\n",
        )

    def test_source_file_swap_to_fifo_is_nonblocking_and_rejected(self) -> None:
        source_file = (
            self.release_b / "personal_codex" / "skills" / "new" / "SKILL.md"
        )
        relative_path = PurePosixPath(
            "personal_codex/skills/new/SKILL.md"
        )
        source_parent_fd, source_fd, _snapshot = MODULE._open_release_source_root(
            self.release_b
        )
        real_open = MODULE.os.open
        swapped = False
        used_nonblock = False

        def open_after_fifo_swap(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped, used_nonblock
            if Path(path).name == source_file.name and not swapped:
                swapped = True
                used_nonblock = bool(flags & getattr(os, "O_NONBLOCK", 0))
                source_file.unlink()
                os.mkfifo(source_file)
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        try:
            with mock.patch.object(MODULE.os, "open", side_effect=open_after_fifo_swap):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "not a regular file",
                ):
                    MODULE._read_regular_file_at(
                        source_fd,
                        relative_path,
                        self.release_b,
                    )
        finally:
            os.close(source_fd)
            os.close(source_parent_fd)

        self.assertTrue(swapped)
        self.assertTrue(used_nonblock)

    def test_release_identity_revalidates_one_coherent_tree_snapshot(self) -> None:
        source_file = (
            self.release_b / "personal_codex" / "skills" / "new" / "SKILL.md"
        )
        source_parent_fd, source_fd, _snapshot = MODULE._open_release_source_root(
            self.release_b
        )
        real_parse = MODULE._parse_manifest_data
        mutated = False

        def parse_then_mutate(
            data: dict[str, object],
            path_kind: object,
        ) -> MODULE.ManifestData:
            nonlocal mutated
            manifest = real_parse(data, path_kind)
            source_file.write_text("# Changed after snapshot\n", encoding="utf-8")
            mutated = True
            return manifest

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_release_path_kind_at_fd",
                    side_effect=AssertionError("live path kind lookup"),
                ),
                mock.patch.object(
                    MODULE,
                    "_parse_manifest_data",
                    side_effect=parse_then_mutate,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "changed during identity validation",
                ),
            ):
                MODULE._release_tree_identity_from_directory_fd(
                    source_fd,
                    self.release_b,
                )
        finally:
            os.close(source_fd)
            os.close(source_parent_fd)

        self.assertTrue(mutated)

    def test_same_inode_source_mutation_aborts_even_when_mtime_is_restored(
        self,
    ) -> None:
        releases = self.home / "personal-sync" / "releases"
        source_file = (
            self.release_b / "personal_codex" / "skills" / "new" / "SKILL.md"
        )
        source_metadata = source_file.stat()
        source_identity = (source_metadata.st_dev, source_metadata.st_ino)
        real_copy = MODULE._copy_bytes
        injected = False

        def mutate_then_copy(
            source_fd: int,
            destination_fd: int,
            expected_size: int,
            display_path: Path,
        ) -> None:
            nonlocal injected
            metadata = os.fstat(source_fd)
            if not injected and (metadata.st_dev, metadata.st_ino) == source_identity:
                injected = True
                source_file.write_bytes(b"# Mutated\n")
                os.utime(
                    source_file,
                    ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
                )
            real_copy(source_fd, destination_fd, expected_size, display_path)

        with mock.patch.object(
            MODULE,
            "_copy_bytes",
            side_effect=mutate_then_copy,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "release source changed"):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertFalse((releases / SHA_B).exists())
        self.assertEqual(len(list(releases.glob(f".retained-{SHA_B}-*"))), 1)

    def test_growing_source_file_is_rejected_after_captured_size(self) -> None:
        releases = self.home / "personal-sync" / "releases"
        source_file = (
            self.release_b / "personal_codex" / "skills" / "new" / "SKILL.md"
        )
        real_copy = MODULE._copy_bytes
        injected = False

        def grow_then_copy(
            source_fd: int,
            destination_fd: int,
            expected_size: int,
            display_path: Path,
        ) -> None:
            nonlocal injected
            if not injected and display_path == source_file:
                injected = True
                with source_file.open("ab") as file:
                    file.write(b"# Appended during copy\n")
            real_copy(source_fd, destination_fd, expected_size, display_path)

        with mock.patch.object(
            MODULE,
            "_copy_bytes",
            side_effect=grow_then_copy,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release source grew during copy",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertFalse((releases / SHA_B).exists())
        self.assertEqual(len(list(releases.glob(f".retained-{SHA_B}-*"))), 1)

    def test_staged_manifest_must_match_complete_preflight_payload(self) -> None:
        releases = self.home / "personal-sync" / "releases"
        real_copy = MODULE._copy_tree_from_directory_fd
        injected = False

        def alter_staged_manifest(
            source_fd: int,
            destination_fd: int,
            display_root: Path,
            relative_root: PurePosixPath,
            source_snapshots: dict[
                PurePosixPath,
                MODULE._ReleaseSourceSnapshot,
            ],
            source_members: dict[PurePosixPath, tuple[str, ...]],
        ) -> None:
            nonlocal injected
            real_copy(
                source_fd,
                destination_fd,
                display_root,
                relative_root,
                source_snapshots,
                source_members,
            )
            if injected or relative_root.parts:
                return
            injected = True
            personal_fd = os.open(
                "personal_codex",
                MODULE._source_directory_flags(),
                dir_fd=destination_fd,
            )
            manifest_fd = -1
            try:
                manifest_fd = os.open(
                    "sync-manifest.json",
                    os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=personal_fd,
                )
                payload = json.loads(
                    (self.release_b / MODULE.MANIFEST_RELATIVE_PATH).read_text(
                        encoding="utf-8"
                    )
                )
                payload["reference_only"] = []
                os.write(manifest_fd, (json.dumps(payload) + "\n").encode("utf-8"))
                os.fsync(manifest_fd)
            finally:
                if manifest_fd >= 0:
                    os.close(manifest_fd)
                os.close(personal_fd)

        with mock.patch.object(
            MODULE,
            "_copy_tree_from_directory_fd",
            side_effect=alter_staged_manifest,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "staged release manifest differs",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertFalse((releases / SHA_B).exists())
        self.assertEqual(len(list(releases.glob(f".retained-{SHA_B}-*"))), 1)

    def test_release_copy_sanitizes_file_and_directory_modes(self) -> None:
        self.release_b.chmod(0o7777)
        personal_codex = self.release_b / "personal_codex"
        skill = personal_codex / "skills" / "new"
        manifest = self.release_b / MODULE.MANIFEST_RELATIVE_PATH
        personal_codex.chmod(0o3777)
        skill.chmod(0o2777)
        (skill / "SKILL.md").chmod(0o6777)
        manifest.chmod(0o666)

        install_quietly(self.release_b, self.home, SHA_B)

        installed = self.home / "personal-sync" / "releases" / SHA_B
        expected_modes = {
            installed: 0o755,
            installed / "personal_codex": 0o755,
            installed / "personal_codex" / "skills" / "new": 0o755,
            installed / "personal_codex" / "skills" / "new" / "SKILL.md": 0o755,
            installed / MODULE.MANIFEST_RELATIVE_PATH: 0o644,
        }
        for path, expected_mode in expected_modes.items():
            with self.subTest(path=path):
                self.assertEqual(path.stat().st_mode & 0o7777, expected_mode)

    def test_installed_release_rejects_modes_that_are_not_sanitized(self) -> None:
        installed = self.home / "personal-sync" / "releases" / SHA_A
        cases = (
            (installed, 0o777),
            (installed / "personal_codex", 0o777),
            (
                installed
                / "personal_codex"
                / "skills"
                / "old"
                / "SKILL.md",
                0o666,
            ),
        )
        for path, unsafe_mode in cases:
            with self.subTest(path=path):
                original_mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(unsafe_mode)
                try:
                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "mode is not sanitized",
                    ):
                        MODULE._installed_release_identity(
                            self.home,
                            MODULE.PUBLIC_OWNER,
                            SHA_A,
                        )
                finally:
                    path.chmod(original_mode)

    def test_post_apply_verification_failure_restores_install_state(self) -> None:
        before = self._snapshot()

        with mock.patch.object(
            MODULE,
            "_verify_desired_entries",
            side_effect=MODULE.SyncError("injected verification failure"),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "injected verification failure"):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertEqual(self._snapshot(), before)

    def test_precommit_link_disappearance_rolls_back_install(self) -> None:
        before = self._snapshot()
        target = self.home / "skills" / "example"
        real_verify = MODULE._verify_managed_link_snapshots
        injected = False

        def remove_link_then_verify(
            home: Path,
            state: MODULE.ManagedState,
            snapshots: dict[PurePosixPath, MODULE.SymlinkSnapshot],
        ) -> None:
            nonlocal injected
            if not injected:
                injected = True
                target.unlink()
            real_verify(home, state, snapshots)

        with mock.patch.object(
            MODULE,
            "_verify_managed_link_snapshots",
            side_effect=remove_link_then_verify,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed link identity changed",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(self._snapshot(), before)

    def test_post_write_failure_before_marker_rolls_back_exactly(self) -> None:
        before_state, before_state_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        current = MODULE._current_link(self.home)
        target = self.home / "skills" / "example"
        current_before = MODULE._read_symlink_snapshot_beneath(self.home, current)
        target_before = MODULE._read_symlink_snapshot_beneath(self.home, target)
        pending_pointer = MODULE._pending_link_pointer_path(self.home)
        real_publish_pointer = MODULE._publish_pending_link_pointer
        real_write = MODULE._write_managed_state
        captured_batch: MODULE.PendingLinkBatch | None = None

        def capture_pending_batch(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> None:
            nonlocal captured_batch
            captured_batch = batch
            real_publish_pointer(home, batch)

        def write_then_fail(
            home: Path,
            state: MODULE.ManagedState,
            transaction: MODULE.ManagedStateFileTransaction | None = None,
        ) -> None:
            real_write(home, state, transaction)
            raise MODULE.SyncError("injected post-write precommit failure")

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_link_pointer",
                side_effect=capture_pending_batch,
            ),
            mock.patch.object(
                MODULE,
                "_write_managed_state",
                side_effect=write_then_fail,
            ),
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                wraps=MODULE._publish_pending_commit_marker,
            ) as publish_marker,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected post-write precommit failure",
            ),
        ):
            install_quietly(self.release_b, self.home, SHA_B)

        publish_marker.assert_not_called()
        self.assertIsNotNone(captured_batch)
        assert captured_batch is not None
        recovered_state, recovered_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        self.assertEqual(recovered_state, before_state)
        self.assertTrue(
            MODULE._managed_state_snapshot_exact(
                recovered_snapshot,
                before_state_snapshot,
            )
        )
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(self.home, current),
            current_before,
        )
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(self.home, target),
            target_before,
        )
        self.assertFalse(os.path.lexists(pending_pointer))
        marker = captured_batch.batch_root / Path(
            *captured_batch.commit_marker_path.parts
        )
        self.assertFalse(os.path.lexists(marker))

    def test_post_replace_state_write_failure_retains_exact_commit_marker(
        self,
    ) -> None:
        real_publish_marker = MODULE._publish_pending_commit_marker
        pending_pointer = MODULE._pending_link_pointer_path(self.home)

        def publish_marker_then_fail(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> None:
            real_publish_marker(home, batch)
            raise MODULE.SyncError("injected post-write failure")

        with mock.patch.object(
            MODULE,
            "_publish_pending_commit_marker",
            side_effect=publish_marker_then_fail,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(pending_pointer.is_file())
        install_quietly(self.release_b, self.home, SHA_B)
        self.assertFalse(pending_pointer.exists())
        self.assertEqual(
            os.readlink(self.home / "personal-sync" / "current"),
            f"releases/{SHA_B}",
        )
        self.assertEqual(
            os.readlink(self.home / "skills" / "example"),
            "../personal-sync/current/personal_codex/skills/new",
        )
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_B},
        )

    def test_overlay_uninstall_post_write_failure_recovers_exact_commit(
        self,
    ) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        public_current = self.home / "personal-sync" / "current"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_target = self.home / "skills" / "private"
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        pending_pointer = MODULE._pending_link_pointer_path(self.home)
        real_publish_marker = MODULE._publish_pending_commit_marker

        def publish_marker_then_fail(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> None:
            real_publish_marker(home, batch)
            raise MODULE.SyncError("injected uninstall post-write failure")

        with mock.patch.object(
            MODULE,
            "_publish_pending_commit_marker",
            side_effect=publish_marker_then_fail,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(pending_pointer.is_file())
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)
        self.assertFalse(pending_pointer.exists())
        self.assertEqual(os.readlink(public_current), f"releases/{SHA_A}")
        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(private_target))
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_A},
        )
        self.assertTrue(state_path.is_file())

    def test_overlay_uninstall_removes_current_before_terminal_state(self) -> None:
        self._install_private_for_link_race()
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_key = PurePosixPath("skills/private")
        observed_states: list[MODULE.ManagedState] = []
        real_move = MODULE._atomic_move_beneath_home

        def observe_state_before_current_move(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            if source == private_current:
                observed_states.append(MODULE._load_managed_state(home))
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=observe_state_before_current_move,
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(len(observed_states), 1)
        self.assertIn("private", observed_states[0].owners)
        self.assertIn(private_key, observed_states[0].links)
        final_state = MODULE._load_managed_state(self.home)
        self.assertNotIn("private", final_state.owners)
        self.assertNotIn(private_key, final_state.links)
        self.assertFalse(os.path.lexists(private_current))

    def test_overlay_uninstall_rolls_back_current_when_state_is_still_before(
        self,
    ) -> None:
        private_target = self._install_private_for_link_race()
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        before_state = MODULE._load_managed_state(self.home)
        before_current = MODULE._read_symlink_snapshot_beneath(
            self.home,
            private_current,
        )
        before_target = MODULE._read_symlink_snapshot_beneath(
            self.home,
            private_target,
        )

        with mock.patch.object(
            MODULE,
            "_write_managed_state",
            side_effect=MODULE.SyncError("injected pre-commit crash"),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "injected pre-commit crash"):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(MODULE._load_managed_state(self.home), before_state)
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_current,
            ).link_identity,
            before_current.link_identity,
        )
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_target,
            ).link_identity,
            before_target.link_identity,
        )

    def test_uninstall_owner_shas_ignore_manifest_compatible_current_aba(
        self,
    ) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        sha_y = "c" * 40
        release_y = self.home / "personal-sync" / "releases" / sha_y
        manifest_y = write_skill_release(release_y, source_name="old")
        self.assertEqual(
            manifest_y,
            MODULE._current_manifest_data(self.home, MODULE.PUBLIC_OWNER),
        )
        public_current = self.home / "personal-sync" / "current"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        real_move = MODULE._atomic_move_beneath_home
        real_current_sha = MODULE._current_sha
        mapping_phase = False
        injected = False

        def move_then_enter_mapping_phase(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            nonlocal mapping_phase
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )
            if source == private_current:
                mapping_phase = True

        def current_sha_with_aba(
            home: Path,
            owner: str = MODULE.PUBLIC_OWNER,
        ) -> str | None:
            nonlocal injected
            if (
                mapping_phase
                and not injected
                and owner == MODULE.PUBLIC_OWNER
                and os.readlink(public_current) == f"releases/{SHA_A}"
            ):
                injected = True
                public_current.unlink()
                public_current.symlink_to(
                    f"releases/{sha_y}",
                    target_is_directory=True,
                )
                try:
                    return real_current_sha(home, owner)
                finally:
                    public_current.unlink()
                    public_current.symlink_to(
                        f"releases/{SHA_A}",
                        target_is_directory=True,
                    )
            return real_current_sha(home, owner)

        with (
            mock.patch.object(
                MODULE,
                "_atomic_move_beneath_home",
                side_effect=move_then_enter_mapping_phase,
            ),
            mock.patch.object(
                MODULE,
                "_current_sha",
                side_effect=current_sha_with_aba,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(injected)
        self.assertEqual(os.readlink(public_current), f"releases/{SHA_A}")
        self.assertFalse(os.path.lexists(private_current))
        self.assertEqual(state["owners"], {MODULE.PUBLIC_OWNER: SHA_A})
        self.assertEqual(
            {link["release_sha"] for link in state["links"]},
            {SHA_A},
        )

    def test_overlay_uninstall_remaining_owner_mutation_rolls_back(self) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        public_current = self.home / "personal-sync" / "current"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_target = self.home / "skills" / "private"
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        public_skill = (
            self.home
            / "personal-sync"
            / "releases"
            / SHA_A
            / "personal_codex"
            / "skills"
            / "old"
            / "SKILL.md"
        )
        before = (
            os.readlink(public_current),
            os.readlink(private_current),
            os.readlink(private_target),
            state_path.read_bytes(),
            state_path.stat().st_mode & 0o777,
        )
        real_verify = MODULE._verify_install_release_identities
        injected = False

        def mutate_then_verify_public(
            home: Path,
            bindings: list[MODULE.InstallReleaseBinding],
            *,
            phase: str,
            verify_current: bool,
        ) -> None:
            nonlocal injected
            if (
                not injected
                and phase == "during final overlay uninstall state validation"
            ):
                injected = True
                metadata = public_skill.stat()
                public_skill.write_bytes(b"# Changed\n")
                public_skill.chmod(stat.S_IMODE(metadata.st_mode))
                os.utime(
                    public_skill,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                )
            real_verify(
                home,
                bindings,
                phase=phase,
                verify_current=verify_current,
            )

        with mock.patch.object(
            MODULE,
            "_verify_install_release_identities",
            side_effect=mutate_then_verify_public,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "release tree changed during final overlay uninstall state validation",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        after = (
            os.readlink(public_current),
            os.readlink(private_current),
            os.readlink(private_target),
            state_path.read_bytes(),
            state_path.stat().st_mode & 0o777,
        )
        self.assertTrue(injected)
        self.assertEqual(after, before)
        self.assertEqual(public_skill.read_bytes(), b"# Changed\n")

    def test_overlay_uninstall_preserves_link_mutated_during_release_scan(
        self,
    ) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        public_current = self.home / "personal-sync" / "current"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        public_target = self.home / "skills" / "example"
        private_target = self.home / "skills" / "private"
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        before = (
            os.readlink(public_current),
            os.readlink(private_current),
            os.readlink(private_target),
            state_path.read_bytes(),
        )
        real_identity = MODULE._release_tree_identity_from_directory_fd
        injected = False

        def scan_then_mutate_public_link(
            root_fd: int,
            display_root: Path,
            *,
            require_sanitized_modes: bool = False,
        ) -> MODULE.ReleaseTreeIdentity:
            nonlocal injected
            identity = real_identity(
                root_fd,
                display_root,
                require_sanitized_modes=require_sanitized_modes,
            )
            if not injected and not os.path.lexists(private_current):
                injected = True
                public_target.unlink()
                public_target.symlink_to("concurrent-drift", target_is_directory=True)
            return identity

        with mock.patch.object(
            MODULE,
            "_release_tree_identity_from_directory_fd",
            side_effect=scan_then_mutate_public_link,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(injected)
        self.assertEqual(os.readlink(public_current), before[0])
        self.assertEqual(os.readlink(private_current), before[1])
        self.assertEqual(os.readlink(private_target), before[2])
        self.assertEqual(state_path.read_bytes(), before[3])
        self.assertEqual(os.readlink(public_target), "concurrent-drift")
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_overlay_uninstall_preserves_same_target_link_inode_racer(
        self,
    ) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        public_target = self.home / "skills" / "example"
        displaced_public_target = self.root / "uninstall-original-link"
        private_target = self.home / "skills" / "private"
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        before = (
            os.readlink(private_current),
            os.readlink(private_target),
            state_path.read_bytes(),
        )
        real_identity = MODULE._release_tree_identity_from_directory_fd
        injected = False
        raced_snapshot: tuple[int, int, str] | None = None

        def scan_then_replace_public_link_inode(
            root_fd: int,
            display_root: Path,
            *,
            require_sanitized_modes: bool = False,
        ) -> MODULE.ReleaseTreeIdentity:
            nonlocal injected, raced_snapshot
            identity = real_identity(
                root_fd,
                display_root,
                require_sanitized_modes=require_sanitized_modes,
            )
            if not injected and not os.path.lexists(private_current):
                injected = True
                same_target = os.readlink(public_target)
                public_target.rename(displaced_public_target)
                public_target.symlink_to(same_target, target_is_directory=True)
                metadata = public_target.lstat()
                raced_snapshot = (
                    metadata.st_dev,
                    metadata.st_ino,
                    same_target,
                )
            return identity

        with mock.patch.object(
            MODULE,
            "_release_tree_identity_from_directory_fd",
            side_effect=scan_then_replace_public_link_inode,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(injected)
        assert raced_snapshot is not None
        target_metadata = public_target.lstat()
        self.assertEqual(os.readlink(private_current), before[0])
        self.assertEqual(os.readlink(private_target), before[1])
        self.assertEqual(state_path.read_bytes(), before[2])
        self.assertEqual(
            (
                target_metadata.st_dev,
                target_metadata.st_ino,
                os.readlink(public_target),
            ),
            raced_snapshot,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())


class ManagedStatePlanningAndAdoptionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.public_release = self.root / "public-release"
        write_skill_release(
            self.public_release,
            source_name="public-base",
            target_name="public-base",
        )
        install_quietly(self.public_release, self.home, SHA_A)
        self.state_path = (
            self.home / "personal-sync" / "state" / "managed-links.json"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install_private(self) -> Path:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        return private_release

    def _concurrent_state_payload(self) -> bytes:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        payload["concurrent_marker"] = "preserve"
        return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

    def _replace_state_file_with_same_content(
        self,
        label: str,
    ) -> tuple[Path, tuple[int, int]]:
        payload = self.state_path.read_bytes()
        mode = self.state_path.stat().st_mode & 0o777
        displaced = self.state_path.with_name(f"{self.state_path.name}-{label}")
        self.state_path.rename(displaced)
        self.state_path.write_bytes(payload)
        self.state_path.chmod(mode)
        metadata = self.state_path.stat()
        return displaced, (metadata.st_dev, metadata.st_ino)

    def _replace_state_parent_with_same_content(
        self,
        label: str,
    ) -> tuple[Path, tuple[int, int], tuple[int, int]]:
        parent = self.state_path.parent
        payload = self.state_path.read_bytes()
        file_mode = self.state_path.stat().st_mode & 0o777
        parent_mode = parent.stat().st_mode & 0o777
        displaced = parent.with_name(f"{parent.name}-{label}")
        parent.rename(displaced)
        parent.mkdir(mode=parent_mode)
        self.state_path.write_bytes(payload)
        self.state_path.chmod(file_mode)
        parent_metadata = parent.stat()
        file_metadata = self.state_path.stat()
        return (
            displaced,
            (parent_metadata.st_dev, parent_metadata.st_ino),
            (file_metadata.st_dev, file_metadata.st_ino),
        )

    def _replace_state_parent_with_same_file_inode(
        self,
        label: str,
    ) -> tuple[Path, tuple[int, int], tuple[int, int]]:
        parent = self.state_path.parent
        parent_mode = stat.S_IMODE(parent.stat().st_mode)
        original_file_identity = (
            self.state_path.stat().st_dev,
            self.state_path.stat().st_ino,
        )
        displaced = parent.with_name(f"{parent.name}-{label}")
        parent.rename(displaced)
        parent.mkdir(mode=parent_mode)
        os.link(displaced / self.state_path.name, self.state_path)
        parent_metadata = parent.stat()
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            original_file_identity,
        )
        return (
            displaced,
            (parent_metadata.st_dev, parent_metadata.st_ino),
            original_file_identity,
        )

    def test_install_does_not_overwrite_state_changed_after_planning(self) -> None:
        next_release = self.root / "next-public-release"
        write_skill_release(
            next_release,
            source_name="public-next",
            target_name="public-base",
        )
        current = self.home / "personal-sync" / "current"
        target = self.home / "skills" / "public-base"
        before = (
            os.readlink(current),
            os.readlink(target),
            self.state_path.stat().st_mode & 0o777,
        )
        concurrent_payload = self._concurrent_state_payload()
        real_prepare = MODULE._prepare_managed_state_transaction
        injected = False

        def prepare_after_concurrent_write(
            home: Path,
            state: MODULE.ManagedState,
            before_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> MODULE.ManagedStateFileTransaction:
            nonlocal injected
            injected = True
            self.state_path.write_bytes(concurrent_payload)
            return real_prepare(home, state, before_snapshot)

        with mock.patch.object(
            MODULE,
            "_prepare_managed_state_transaction",
            side_effect=prepare_after_concurrent_write,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete",
            ):
                install_quietly(next_release, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertEqual(os.readlink(current), before[0])
        self.assertEqual(os.readlink(target), before[1])
        self.assertEqual(self.state_path.read_bytes(), concurrent_payload)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, before[2])
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_uninstall_does_not_overwrite_state_changed_after_planning(self) -> None:
        self._install_private()
        public_target = self.home / "skills" / "public-base"
        private_target = self.home / "skills" / "private"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        before = (
            os.readlink(public_target),
            os.readlink(private_target),
            os.readlink(private_current),
            self.state_path.stat().st_mode & 0o777,
        )
        concurrent_payload = self._concurrent_state_payload()
        real_prepare = MODULE._prepare_managed_state_transaction
        injected = False

        def prepare_after_concurrent_write(
            home: Path,
            state: MODULE.ManagedState,
            before_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> MODULE.ManagedStateFileTransaction:
            nonlocal injected
            injected = True
            self.state_path.write_bytes(concurrent_payload)
            return real_prepare(home, state, before_snapshot)

        with mock.patch.object(
            MODULE,
            "_prepare_managed_state_transaction",
            side_effect=prepare_after_concurrent_write,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(injected)
        self.assertEqual(os.readlink(public_target), before[0])
        self.assertEqual(os.readlink(private_target), before[1])
        self.assertEqual(os.readlink(private_current), before[2])
        self.assertEqual(self.state_path.read_bytes(), concurrent_payload)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, before[3])
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_install_rejects_same_content_state_inode_swap_after_planning(
        self,
    ) -> None:
        next_release = self.root / "next-public-release-same-ledger"
        write_skill_release(
            next_release,
            source_name="public-next",
            target_name="public-base",
        )
        current = self.home / "personal-sync" / "current"
        target = self.home / "skills" / "public-base"
        before = (os.readlink(current), os.readlink(target))
        real_prepare = MODULE._prepare_managed_state_transaction
        displaced: Path | None = None
        replacement_identity: tuple[int, int] | None = None

        def prepare_after_same_content_inode_swap(
            home: Path,
            state: MODULE.ManagedState,
            before_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> MODULE.ManagedStateFileTransaction:
            nonlocal displaced, replacement_identity
            displaced, replacement_identity = self._replace_state_file_with_same_content(
                "install-original"
            )
            return real_prepare(home, state, before_snapshot)

        with mock.patch.object(
            MODULE,
            "_prepare_managed_state_transaction",
            side_effect=prepare_after_same_content_inode_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete",
            ):
                install_quietly(next_release, self.home, SHA_B)

        self.assertEqual(os.readlink(current), before[0])
        self.assertEqual(os.readlink(target), before[1])
        self.assertIsNotNone(displaced)
        self.assertIsNotNone(replacement_identity)
        assert displaced is not None
        assert replacement_identity is not None
        self.assertEqual(self.state_path.read_bytes(), displaced.read_bytes())
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            replacement_identity,
        )
        self.assertNotEqual(
            (displaced.stat().st_dev, displaced.stat().st_ino),
            replacement_identity,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_install_rejects_same_content_state_parent_swap_after_planning(
        self,
    ) -> None:
        next_release = self.root / "next-public-release-same-parent"
        write_skill_release(
            next_release,
            source_name="public-next",
            target_name="public-base",
        )
        current = self.home / "personal-sync" / "current"
        target = self.home / "skills" / "public-base"
        before = (os.readlink(current), os.readlink(target))
        real_prepare = MODULE._prepare_managed_state_transaction
        displaced_parent: Path | None = None
        replacement_parent_identity: tuple[int, int] | None = None
        replacement_file_identity: tuple[int, int] | None = None

        def prepare_after_same_content_parent_swap(
            home: Path,
            state: MODULE.ManagedState,
            before_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> MODULE.ManagedStateFileTransaction:
            nonlocal displaced_parent
            nonlocal replacement_parent_identity, replacement_file_identity
            (
                displaced_parent,
                replacement_parent_identity,
                replacement_file_identity,
            ) = self._replace_state_parent_with_same_content("install-original")
            return real_prepare(home, state, before_snapshot)

        with mock.patch.object(
            MODULE,
            "_prepare_managed_state_transaction",
            side_effect=prepare_after_same_content_parent_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete",
            ):
                install_quietly(next_release, self.home, SHA_B)

        self.assertEqual(os.readlink(current), before[0])
        self.assertEqual(os.readlink(target), before[1])
        self.assertIsNotNone(displaced_parent)
        self.assertIsNotNone(replacement_parent_identity)
        self.assertIsNotNone(replacement_file_identity)
        assert displaced_parent is not None
        assert replacement_parent_identity is not None
        assert replacement_file_identity is not None
        self.assertEqual(
            (self.state_path.parent.stat().st_dev, self.state_path.parent.stat().st_ino),
            replacement_parent_identity,
        )
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            replacement_file_identity,
        )
        self.assertEqual(
            self.state_path.read_bytes(),
            (displaced_parent / self.state_path.name).read_bytes(),
        )
        self.assertNotEqual(
            (displaced_parent.stat().st_dev, displaced_parent.stat().st_ino),
            replacement_parent_identity,
        )
        displaced_state = displaced_parent / self.state_path.name
        self.assertNotEqual(
            (displaced_state.stat().st_dev, displaced_state.stat().st_ino),
            replacement_file_identity,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_install_rejects_state_parent_swap_with_same_inode_after_release_staging(
        self,
    ) -> None:
        next_release = self.root / "next-public-release-staging-parent"
        write_skill_release(
            next_release,
            source_name="public-next",
            target_name="public-base",
        )
        current = MODULE._current_link(self.home)
        target = self.home / "skills" / "public-base"
        before_links = (os.readlink(current), os.readlink(target))
        real_stage = MODULE._stage_release_tree_for_install
        displaced_parent: Path | None = None
        replacement_parent_identity: tuple[int, int] | None = None
        shared_file_identity: tuple[int, int] | None = None

        def stage_then_swap_parent(
            source_root: Path,
            home: Path,
            sha: str,
            manifest: MODULE.ManifestData,
            source_expectation: MODULE.ReleaseTreeExpectation | None = None,
        ) -> MODULE.InstallReleaseBinding:
            nonlocal displaced_parent
            nonlocal replacement_parent_identity, shared_file_identity
            binding = real_stage(
                source_root,
                home,
                sha,
                manifest,
                source_expectation,
            )
            if displaced_parent is None:
                (
                    displaced_parent,
                    replacement_parent_identity,
                    shared_file_identity,
                ) = self._replace_state_parent_with_same_file_inode(
                    "after-release-staging"
                )
            return binding

        with mock.patch.object(
            MODULE,
            "_stage_release_tree_for_install",
            side_effect=stage_then_swap_parent,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed state snapshot changed before pending transaction staging",
            ):
                install_quietly(next_release, self.home, SHA_B)

        self.assertIsNotNone(displaced_parent)
        self.assertIsNotNone(replacement_parent_identity)
        self.assertIsNotNone(shared_file_identity)
        assert displaced_parent is not None
        assert replacement_parent_identity is not None
        assert shared_file_identity is not None
        self.assertEqual((os.readlink(current), os.readlink(target)), before_links)
        self.assertEqual(
            (self.state_path.parent.stat().st_dev, self.state_path.parent.stat().st_ino),
            replacement_parent_identity,
        )
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            shared_file_identity,
        )
        self.assertEqual(
            (
                (displaced_parent / self.state_path.name).stat().st_dev,
                (displaced_parent / self.state_path.name).stat().st_ino,
            ),
            shared_file_identity,
        )
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )

    def test_install_rejects_bound_missing_state_parent_swap_after_release_staging(
        self,
    ) -> None:
        next_release = self.root / "next-public-release-missing-state-parent"
        write_skill_release(
            next_release,
            source_name="public-next",
            target_name="public-base",
        )
        self.state_path.unlink()
        original_parent_identity = (
            self.state_path.parent.stat().st_dev,
            self.state_path.parent.stat().st_ino,
        )
        real_stage = MODULE._stage_release_tree_for_install
        displaced_parent: Path | None = None
        replacement_parent_identity: tuple[int, int] | None = None

        def stage_then_swap_missing_parent(
            source_root: Path,
            home: Path,
            sha: str,
            manifest: MODULE.ManifestData,
            source_expectation: MODULE.ReleaseTreeExpectation | None = None,
        ) -> MODULE.InstallReleaseBinding:
            nonlocal displaced_parent, replacement_parent_identity
            binding = real_stage(
                source_root,
                home,
                sha,
                manifest,
                source_expectation,
            )
            if displaced_parent is None:
                parent = self.state_path.parent
                parent_mode = stat.S_IMODE(parent.stat().st_mode)
                displaced_parent = parent.with_name(
                    f"{parent.name}-missing-after-release-staging"
                )
                parent.rename(displaced_parent)
                parent.mkdir(mode=parent_mode)
                metadata = parent.stat()
                replacement_parent_identity = (metadata.st_dev, metadata.st_ino)
            return binding

        with mock.patch.object(
            MODULE,
            "_stage_release_tree_for_install",
            side_effect=stage_then_swap_missing_parent,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "managed state snapshot changed before pending transaction staging",
            ):
                install_quietly(next_release, self.home, SHA_B)

        self.assertIsNotNone(displaced_parent)
        self.assertIsNotNone(replacement_parent_identity)
        assert replacement_parent_identity is not None
        self.assertNotEqual(replacement_parent_identity, original_parent_identity)
        self.assertFalse(os.path.lexists(self.state_path))
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )

    def test_managed_state_staging_snapshot_transition_rules(self) -> None:
        first_bound = (1, 2)
        second_bound = (3, 4)
        missing_unbound = MODULE.ManagedStateFileSnapshot(exists=False)
        missing_first = MODULE.ManagedStateFileSnapshot(
            exists=False,
            parent_identity=first_bound,
        )
        missing_second = MODULE.ManagedStateFileSnapshot(
            exists=False,
            parent_identity=second_bound,
        )
        existing_first = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"{}\n",
            mode=0o600,
            parent_identity=first_bound,
            file_identity=(5, 6),
        )
        existing_second_parent = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"{}\n",
            mode=0o600,
            parent_identity=second_bound,
            file_identity=(5, 6),
        )

        self.assertTrue(
            MODULE._managed_state_staging_snapshot_transition_is_allowed(
                missing_unbound,
                missing_first,
                first_bound,
            )
        )
        self.assertTrue(
            MODULE._managed_state_staging_snapshot_transition_is_allowed(
                missing_first,
                missing_first,
                first_bound,
            )
        )
        self.assertFalse(
            MODULE._managed_state_staging_snapshot_transition_is_allowed(
                missing_first,
                missing_second,
                second_bound,
            )
        )
        self.assertFalse(
            MODULE._managed_state_staging_snapshot_transition_is_allowed(
                existing_first,
                existing_second_parent,
                second_bound,
            )
        )

    def test_uninstall_rejects_same_content_state_inode_swap_after_planning(
        self,
    ) -> None:
        self._install_private()
        public_target = self.home / "skills" / "public-base"
        private_target = self.home / "skills" / "private"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        before = (
            os.readlink(public_target),
            os.readlink(private_target),
            os.readlink(private_current),
        )
        real_prepare = MODULE._prepare_managed_state_transaction
        displaced: Path | None = None
        replacement_identity: tuple[int, int] | None = None

        def prepare_after_same_content_inode_swap(
            home: Path,
            state: MODULE.ManagedState,
            before_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> MODULE.ManagedStateFileTransaction:
            nonlocal displaced, replacement_identity
            displaced, replacement_identity = self._replace_state_file_with_same_content(
                "uninstall-original"
            )
            return real_prepare(home, state, before_snapshot)

        with mock.patch.object(
            MODULE,
            "_prepare_managed_state_transaction",
            side_effect=prepare_after_same_content_inode_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(os.readlink(public_target), before[0])
        self.assertEqual(os.readlink(private_target), before[1])
        self.assertEqual(os.readlink(private_current), before[2])
        self.assertIsNotNone(displaced)
        self.assertIsNotNone(replacement_identity)
        assert displaced is not None
        assert replacement_identity is not None
        self.assertEqual(self.state_path.read_bytes(), displaced.read_bytes())
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            replacement_identity,
        )
        self.assertNotEqual(
            (displaced.stat().st_dev, displaced.stat().st_ino),
            replacement_identity,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_uninstall_rejects_same_content_state_parent_swap_after_planning(
        self,
    ) -> None:
        self._install_private()
        public_target = self.home / "skills" / "public-base"
        private_target = self.home / "skills" / "private"
        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        before = (
            os.readlink(public_target),
            os.readlink(private_target),
            os.readlink(private_current),
        )
        real_prepare = MODULE._prepare_managed_state_transaction
        displaced_parent: Path | None = None
        replacement_parent_identity: tuple[int, int] | None = None
        replacement_file_identity: tuple[int, int] | None = None

        def prepare_after_same_content_parent_swap(
            home: Path,
            state: MODULE.ManagedState,
            before_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> MODULE.ManagedStateFileTransaction:
            nonlocal displaced_parent
            nonlocal replacement_parent_identity, replacement_file_identity
            (
                displaced_parent,
                replacement_parent_identity,
                replacement_file_identity,
            ) = self._replace_state_parent_with_same_content("uninstall-original")
            return real_prepare(home, state, before_snapshot)

        with mock.patch.object(
            MODULE,
            "_prepare_managed_state_transaction",
            side_effect=prepare_after_same_content_parent_swap,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete",
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(os.readlink(public_target), before[0])
        self.assertEqual(os.readlink(private_target), before[1])
        self.assertEqual(os.readlink(private_current), before[2])
        self.assertIsNotNone(displaced_parent)
        self.assertIsNotNone(replacement_parent_identity)
        self.assertIsNotNone(replacement_file_identity)
        assert displaced_parent is not None
        assert replacement_parent_identity is not None
        assert replacement_file_identity is not None
        self.assertEqual(
            (self.state_path.parent.stat().st_dev, self.state_path.parent.stat().st_ino),
            replacement_parent_identity,
        )
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            replacement_file_identity,
        )
        self.assertEqual(
            self.state_path.read_bytes(),
            (displaced_parent / self.state_path.name).read_bytes(),
        )
        self.assertNotEqual(
            (displaced_parent.stat().st_dev, displaced_parent.stat().st_ino),
            replacement_parent_identity,
        )
        displaced_state = displaced_parent / self.state_path.name
        self.assertNotEqual(
            (displaced_state.stat().st_dev, displaced_state.stat().st_ino),
            replacement_file_identity,
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_existing_state_does_not_adopt_untracked_current_link(self) -> None:
        self._install_private()
        private_target = self.home / "skills" / "private"
        private_target_value = os.readlink(private_target)
        private_target.unlink()
        private_target.symlink_to(private_target_value, target_is_directory=True)
        private_identity = (
            os.lstat(private_target).st_dev,
            os.lstat(private_target).st_ino,
        )

        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        payload["links"] = [
            link for link in payload["links"] if link["target"] != "skills/private"
        ]
        self.state_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        state = MODULE._load_managed_state(self.home)
        private_key = PurePosixPath("skills/private")
        self.assertNotIn(private_key, state.links)

        refreshed = MODULE._refresh_managed_state_from_current(
            self.home,
            state,
            bootstrap_history=False,
        )
        self.assertNotIn(private_key, refreshed.links)

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(private_target.is_symlink())
        self.assertEqual(os.readlink(private_target), private_target_value)
        self.assertEqual(
            (os.lstat(private_target).st_dev, os.lstat(private_target).st_ino),
            private_identity,
        )
        final_state = MODULE._load_managed_state(self.home)
        self.assertNotIn(private_key, final_state.links)

    def test_install_does_not_adopt_preexisting_exact_desired_link(self) -> None:
        private_release = self.root / "preexisting-link-private-release"
        private_manifest = write_skill_release(
            private_release,
            source_name="private",
            target_name="private",
            owner="private",
        )
        private_entry = private_manifest.entries[0]
        private_target = MODULE._entry_target_path(self.home, private_entry)
        private_target.parent.mkdir(parents=True, exist_ok=True)
        private_target_value = MODULE._desired_link_target(self.home, private_entry)
        private_target.symlink_to(private_target_value, target_is_directory=True)
        private_identity = (
            os.lstat(private_target).st_dev,
            os.lstat(private_target).st_ino,
        )

        install_quietly(private_release, self.home, SHA_B)

        private_key = PurePosixPath("skills/private")
        installed_state = MODULE._load_managed_state(self.home)
        self.assertEqual(installed_state.owners["private"], SHA_B)
        self.assertNotIn(private_key, installed_state.links)
        self.assertEqual(os.readlink(private_target), private_target_value)
        self.assertEqual(
            (os.lstat(private_target).st_dev, os.lstat(private_target).st_ino),
            private_identity,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(private_target.is_symlink())
        self.assertEqual(os.readlink(private_target), private_target_value)
        self.assertEqual(
            (os.lstat(private_target).st_dev, os.lstat(private_target).st_ino),
            private_identity,
        )
        final_state = MODULE._load_managed_state(self.home)
        self.assertNotIn("private", final_state.owners)
        self.assertNotIn(private_key, final_state.links)

    def test_current_state_sha_mismatch_does_not_adopt_preexisting_exact_link(
        self,
    ) -> None:
        next_release = self.root / "next-public-release"
        write_skill_release(
            next_release,
            source_name="public-base",
            target_name="public-base",
        )
        added_source = next_release / "personal_codex" / "skills" / "added"
        added_source.mkdir(parents=True)
        (added_source / "SKILL.md").write_text("# Added\n", encoding="utf-8")
        manifest_path = next_release / MODULE.MANIFEST_RELATIVE_PATH
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["links"].append(
            {
                "source": "personal_codex/skills/added",
                "target": "skills/added",
                "kind": "skill",
                "owner": MODULE.PUBLIC_OWNER,
            }
        )
        manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        next_manifest = MODULE.load_manifest_data(next_release)

        binding = MODULE._stage_release_tree_for_install(
            next_release,
            self.home,
            SHA_B,
            next_manifest,
        )
        MODULE._close_install_release_bindings([binding])
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE._switch_current(self.home, SHA_B, dry_run=False)

        added_entry = next(
            entry
            for entry in next_manifest.entries
            if entry.target == PurePosixPath("skills/added")
        )
        added_target = MODULE._entry_target_path(self.home, added_entry)
        added_target.parent.mkdir(parents=True, exist_ok=True)
        added_target.symlink_to(
            MODULE._desired_link_target(self.home, added_entry),
            target_is_directory=True,
        )
        added_identity = (
            added_target.lstat().st_dev,
            added_target.lstat().st_ino,
        )
        interrupted_state = MODULE._load_managed_state(self.home)
        self.assertEqual(interrupted_state.owners[MODULE.PUBLIC_OWNER], SHA_A)
        self.assertNotIn(PurePosixPath("skills/added"), interrupted_state.links)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed state/current release mismatch",
        ):
            install_quietly(next_release, self.home, SHA_B)

        final_state = MODULE._load_managed_state(self.home)
        self.assertEqual(final_state.owners[MODULE.PUBLIC_OWNER], SHA_A)
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)
        self.assertNotIn(PurePosixPath("skills/added"), final_state.links)
        self.assertEqual(
            (added_target.lstat().st_dev, added_target.lstat().st_ino),
            added_identity,
        )
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )

    def test_first_bootstrap_adopts_current_legacy_managed_link(self) -> None:
        self.state_path.unlink()
        refreshed = MODULE._refresh_managed_state_from_current(
            self.home,
            MODULE._empty_managed_state(),
            bootstrap_history=True,
        )

        self.assertIn(PurePosixPath("skills/public-base"), refreshed.links)


class PendingLinkTransactionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.release_a = self.root / "release-a"
        self.release_b = self.root / "release-b"
        write_skill_release(
            self.release_a,
            source_name="public-base",
            target_name="public-base",
        )
        write_skill_release(
            self.release_b,
            source_name="public-new",
            target_name="public-base",
        )
        added_source = self.release_b / "personal_codex" / "skills" / "added"
        added_source.mkdir(parents=True)
        (added_source / "SKILL.md").write_text("# Added\n", encoding="utf-8")
        manifest_path = self.release_b / MODULE.MANIFEST_RELATIVE_PATH
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["links"].append(
            {
                "source": "personal_codex/skills/added",
                "target": "skills/added",
                "kind": "skill",
                "owner": MODULE.PUBLIC_OWNER,
            }
        )
        manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.manifest_b = MODULE.load_manifest_data(self.release_b)
        install_quietly(self.release_a, self.home, SHA_A)
        self.state_path = MODULE._state_path(self.home)
        self.pointer_path = MODULE._pending_link_pointer_path(self.home)
        self.added_target = self.home / "skills" / "added"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_quarantine_batch_retention_cap_blocks_unbounded_staging(
        self,
    ) -> None:
        with mock.patch.object(MODULE, "MAX_RETAINED_QUARANTINE_BATCHES", 2):
            first = MODULE._quarantine_batch_root(self.home, [])
            second = MODULE._quarantine_batch_root(self.home, [])
            before = tuple(sorted(path.name for path in first.parent.iterdir()))
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "quarantine retains too many transaction batches: 2 >= 2",
            ):
                MODULE._quarantine_batch_root(self.home, [])

        self.assertEqual(
            tuple(sorted(path.name for path in first.parent.iterdir())),
            before,
        )
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())

    def test_isolated_cleanup_batch_counts_toward_retention_cap(self) -> None:
        with mock.patch.object(MODULE, "MAX_RETAINED_QUARANTINE_BATCHES", 2):
            first = MODULE._quarantine_batch_root(self.home, [])
            isolated = first.with_name(
                MODULE._pending_cleanup_isolated_batch_name(first.name)
            )
            first.rename(isolated)
            second = MODULE._quarantine_batch_root(self.home, [])

            with self.assertRaisesRegex(
                MODULE.SyncError,
                "quarantine retains too many transaction batches: 2 >= 2",
            ):
                MODULE._quarantine_batch_root(self.home, [])

        self.assertTrue(isolated.is_dir())
        self.assertTrue(second.is_dir())

    def _stage_crash_batch(
        self,
    ) -> tuple[
        MODULE.PendingLinkBatch,
        list[MODULE.ReconcileAction],
        MODULE.ManagedState,
        MODULE.ManagedStateFileSnapshot,
    ]:
        binding = MODULE._stage_release_tree_for_install(
            self.release_b,
            self.home,
            SHA_B,
            self.manifest_b,
        )
        MODULE._close_install_release_bindings([binding])
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
        current_manifest = MODULE._current_manifest_data(self.home, MODULE.PUBLIC_OWNER)
        actions = MODULE._plan_reconciliation(
            self.home,
            self.manifest_b.entries,
            current_manifest.entries,
            [],
            state,
            allow_cross_owner=False,
        )
        current_action = MODULE._plan_current_switch_action(
            self.home,
            SHA_B,
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
            self.manifest_b.entries,
            {MODULE.PUBLIC_OWNER: SHA_B},
            managed_targets,
        )
        batch = MODULE._stage_pending_link_batch(
            self.home,
            [("current", [current_action]), ("managed", actions)],
            self.manifest_b.entries,
            {MODULE.PUBLIC_OWNER: SHA_B},
            state_snapshot,
            state,
            next_state,
        )
        MODULE._publish_pending_link_pointer(self.home, batch)
        return batch, actions, state, state_snapshot

    def _publish_pending_target(
        self,
        batch: MODULE.PendingLinkBatch,
        actions: list[MODULE.ReconcileAction],
    ) -> MODULE.ReconcileTransaction:
        current_action = MODULE._plan_current_switch_action(
            self.home,
            SHA_B,
            MODULE.PUBLIC_OWNER,
        )
        self.assertIsNotNone(current_action)
        assert current_action is not None
        current_transaction = MODULE._apply_reconcile_actions(
            self.home,
            [current_action],
            dry_run=False,
            pending_batch=batch,
            pending_scope="current",
            batch_root=batch.batch_root,
        )
        self.assertIsNotNone(current_transaction)
        transaction = MODULE._apply_reconcile_actions(
            self.home,
            actions,
            dry_run=False,
            pending_batch=batch,
            pending_scope="managed",
            batch_root=batch.batch_root,
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        return transaction

    def _publish_committed_state(
        self,
        batch: MODULE.PendingLinkBatch,
        state_snapshot: MODULE.ManagedStateFileSnapshot,
        *,
        mark_committed: bool = True,
    ) -> MODULE.ManagedStateFileTransaction:
        transaction = MODULE._prepare_pending_managed_state_transaction(
            self.home,
            batch,
            batch.state_after_value,
        )
        MODULE._write_managed_state(
            self.home,
            batch.state_after_value,
            transaction,
        )
        if mark_committed:
            MODULE._publish_pending_commit_marker(self.home, batch)
        return transaction

    def _stage_single_added_create_batch(
        self,
    ) -> tuple[
        MODULE.PendingLinkBatch,
        MODULE.ManagedState,
        MODULE.ManagedStateFileSnapshot,
    ]:
        batch, actions, state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        return batch, state, state_snapshot

    def _move_exact_added_inode_under_replaced_parent(
        self,
        batch: MODULE.PendingLinkBatch,
    ) -> tuple[int, int]:
        record = next(
            record
            for record in batch.records
            if record.target == PurePosixPath("skills/added")
        )
        assert record.evidence is not None
        evidence = batch.batch_root / Path(*record.evidence.parts)
        evidence_identity = (evidence.lstat().st_dev, evidence.lstat().st_ino)
        skills = self.added_target.parent
        displaced = self.home / "skills-displaced-by-racer"
        skills.rename(displaced)
        skills.mkdir()
        os.link(
            evidence,
            self.added_target,
            follow_symlinks=False,
        )
        self.assertEqual(
            (self.added_target.lstat().st_dev, self.added_target.lstat().st_ino),
            evidence_identity,
        )
        return evidence_identity

    def _install_with_deferred_cleanup(self) -> tuple[Path, Path, str]:
        captured_batches: list[Path] = []
        real_stage = MODULE._stage_pending_link_batch

        def capture_stage(*args: object, **kwargs: object) -> MODULE.PendingLinkBatch:
            batch = real_stage(*args, **kwargs)
            captured_batches.append(batch.batch_root)
            return batch

        with (
            mock.patch.object(
                MODULE,
                "_stage_pending_link_batch",
                side_effect=capture_stage,
            ),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=MODULE.SyncError("injected cleanup failure"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            MODULE.install_release_tree(
                self.release_b,
                self.home,
                SHA_B,
                dry_run=False,
            )

        self.assertEqual(len(captured_batches), 1)
        batch_root = captured_batches[0]
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        return batch_root, ticket_path, stdout.getvalue()

    def _stage_unclaimed_removal_batch(
        self,
    ) -> tuple[
        MODULE.PendingLinkBatch,
        MODULE.ManagedState,
        MODULE.ManagedStateFileSnapshot,
    ]:
        target = self.home / "skills" / "unclaimed-legacy"
        target.symlink_to("legacy-source", target_is_directory=True)
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
        old_link = MODULE._read_symlink_snapshot_beneath(self.home, target)
        action = MODULE.ReconcileAction(
            "quarantine-remove",
            target,
            "",
            "skill",
            expected_link_target=old_link.link_target,
            planned_snapshot=MODULE._capture_reconcile_target_snapshot(
                self.home,
                target,
            ),
        )
        batch = MODULE._stage_pending_link_batch(
            self.home,
            [("managed", [action])],
            [],
            state.owners,
            state_snapshot,
            state,
            state,
        )
        MODULE._publish_pending_link_pointer(self.home, batch)
        MODULE._apply_reconcile_actions(
            self.home,
            [action],
            dry_run=False,
            pending_batch=batch,
            pending_scope="managed",
            batch_root=batch.batch_root,
        )
        return batch, state, state_snapshot

    def _missing_managed_repair_inputs(
        self,
    ) -> tuple[
        list[MODULE.LinkEntry],
        list[MODULE.ReconcileAction],
        MODULE.ManagedState,
        MODULE.ManagedStateFileSnapshot,
        MODULE.ManagedState,
    ]:
        target = self.home / "skills" / "public-base"
        target.unlink()
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
        manifest = MODULE._current_manifest_data(self.home, MODULE.PUBLIC_OWNER)
        actions = MODULE._plan_reconciliation(
            self.home,
            manifest.entries,
            manifest.entries,
            [],
            state,
            allow_cross_owner=False,
        )
        self.assertEqual(
            [(action.action, action.target) for action in actions],
            [("create", target)],
        )
        managed_targets = MODULE._managed_targets_after_reconciliation(
            self.home,
            state,
            actions,
        )
        next_state = MODULE._planned_committed_state(
            self.home,
            manifest.entries,
            state.owners,
            managed_targets,
        )
        return manifest.entries, actions, state, state_snapshot, next_state

    def test_local_install_recovers_pending_before_reading_source(self) -> None:
        self._stage_crash_batch()

        def source_reached(*_args: object, **_kwargs: object) -> object:
            self.assertFalse(os.path.lexists(self.pointer_path))
            raise RuntimeError("source input reached")

        with (
            mock.patch.object(
                MODULE,
                "_source_release_identity",
                side_effect=source_reached,
            ),
            self.assertRaisesRegex(RuntimeError, "source input reached"),
        ):
            MODULE.install_release_tree(
                self.release_b,
                self.home,
                SHA_B,
                dry_run=False,
            )

    def test_dry_run_stops_before_source_when_pending_recovery_is_required(
        self,
    ) -> None:
        self._stage_crash_batch()

        with (
            mock.patch.object(MODULE, "_source_release_identity") as source_identity,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            MODULE.install_release_tree(
                self.release_b,
                self.home,
                SHA_B,
                dry_run=True,
            )

        source_identity.assert_not_called()
        self.assertTrue(os.path.lexists(self.pointer_path))
        self.assertIn("would recover pending personal sync transaction", stdout.getvalue())

    def test_github_dry_runs_stop_before_download_when_recovery_is_required(
        self,
    ) -> None:
        entrypoints = (
            (
                MODULE.install_from_github,
                ("owner/repo", self.home),
                {"dry_run": True},
            ),
            (
                MODULE.install_private_from_github,
                ("owner/private", self.home),
                {
                    "base_repo": "owner/public",
                    "owner": "private",
                    "dry_run": True,
                },
            ),
        )
        for entrypoint, args, kwargs in entrypoints:
            with self.subTest(entrypoint=entrypoint.__name__):
                self._stage_crash_batch()
                with (
                    mock.patch.object(MODULE, "download_and_extract_release") as download,
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                ):
                    entrypoint(*args, **kwargs)

                download.assert_not_called()
                self.assertTrue(os.path.lexists(self.pointer_path))
                self.assertIn(
                    "would recover pending personal sync transaction",
                    stdout.getvalue(),
                )
                state, state_snapshot = MODULE._load_managed_state_with_snapshot(
                    self.home
                )
                MODULE._recover_pending_link_transaction(
                    self.home,
                    state,
                    state_snapshot,
                    dry_run=False,
                )
                self.assertFalse(os.path.lexists(self.pointer_path))

    def test_pending_probe_rechecks_after_another_process_clears_pointer(
        self,
    ) -> None:
        self._stage_crash_batch()

        @contextlib.contextmanager
        def clear_pending_before_lock(_home: Path) -> Iterator[None]:
            state, state_snapshot = MODULE._load_managed_state_with_snapshot(
                self.home
            )
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )
            yield

        with mock.patch.object(
            MODULE,
            "installation_lock",
            side_effect=clear_pending_before_lock,
        ):
            recovered = MODULE._preflight_pending_recovery(
                self.home,
                dry_run=False,
            )

        self.assertFalse(recovered)
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_github_installs_recover_pending_before_download(self) -> None:
        entrypoints = (
            (
                MODULE.install_from_github,
                ("owner/repo", self.home),
                {"dry_run": False},
            ),
            (
                MODULE.install_private_from_github,
                ("owner/private", self.home),
                {
                    "base_repo": "owner/public",
                    "owner": "private",
                    "dry_run": False,
                },
            ),
        )
        for entrypoint, args, kwargs in entrypoints:
            with self.subTest(entrypoint=entrypoint.__name__):
                self._stage_crash_batch()

                def download_reached(*_args: object, **_kwargs: object) -> object:
                    self.assertFalse(os.path.lexists(self.pointer_path))
                    raise RuntimeError("download reached")

                with (
                    mock.patch.object(
                        MODULE,
                        "download_and_extract_release",
                        side_effect=download_reached,
                    ),
                    self.assertRaisesRegex(RuntimeError, "download reached"),
                ):
                    entrypoint(*args, **kwargs)

    def test_rollback_recovers_pending_before_release_selection(self) -> None:
        self._stage_crash_batch()

        def selection_reached(*_args: object, **_kwargs: object) -> str:
            self.assertFalse(os.path.lexists(self.pointer_path))
            raise RuntimeError("rollback selection reached")

        with (
            mock.patch.object(
                MODULE,
                "_resolve_release_for_rollback",
                side_effect=selection_reached,
            ),
            self.assertRaisesRegex(RuntimeError, "rollback selection reached"),
        ):
            MODULE.rollback(self.home, None)

    def test_stage_only_pending_recovery_clears_pointer_without_adoption(self) -> None:
        batch, _actions, state, state_snapshot = self._stage_crash_batch()
        record = next(record for record in batch.records if record.stage is not None)
        stage = batch.batch_root / Path(*record.stage.parts)
        evidence = batch.batch_root / Path(*record.evidence.parts)
        self.assertEqual(
            (stage.lstat().st_dev, stage.lstat().st_ino),
            (evidence.lstat().st_dev, evidence.lstat().st_ino),
        )

        recovered, _snapshot, did_recover = MODULE._recover_pending_link_transaction(
            self.home,
            state,
            state_snapshot,
            dry_run=False,
        )

        self.assertTrue(did_recover)
        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertFalse(os.path.lexists(self.added_target))
        self.assertNotIn(PurePosixPath("skills/added"), recovered.links)
        self.assertTrue(stage.is_symlink())
        self.assertTrue(evidence.is_symlink())

    def test_unmarked_exact_after_state_is_rolled_back_as_precommit(self) -> None:
        batch, actions, state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(
            batch,
            state_snapshot,
            mark_committed=False,
        )
        after_state, after_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        self.assertEqual(after_state, batch.state_after_value)
        self.assertEqual(after_snapshot.file_identity, batch.state_after.file_identity)

        recovered, recovered_snapshot, did_recover = (
            MODULE._recover_pending_link_transaction(
                self.home,
                after_state,
                after_snapshot,
                dry_run=False,
            )
        )

        self.assertTrue(did_recover)
        self.assertEqual(recovered, state)
        self.assertEqual(recovered_snapshot.file_identity, state_snapshot.file_identity)
        self.assertEqual(MODULE._current_sha(self.home), SHA_A)
        self.assertFalse(os.path.lexists(self.added_target))
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_exact_commit_marker_with_missing_state_fails_closed(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        marker = batch.batch_root / Path(*batch.commit_marker_path.parts)
        evidence = batch.batch_root / Path(*batch.commit_evidence_path.parts)
        self.assertEqual(
            (marker.stat().st_dev, marker.stat().st_ino),
            (evidence.stat().st_dev, evidence.stat().st_ino),
        )
        self.state_path.unlink()
        missing_state, missing_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "committed pending managed state is missing or changed",
        ):
            MODULE._recover_pending_link_transaction(
                self.home,
                missing_state,
                missing_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertFalse(os.path.lexists(self.state_path))
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)
        self.assertTrue(self.added_target.is_symlink())

    def test_committed_release_tree_mutation_blocks_pointer_clear(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        release_dir = (
            MODULE._releases_root(self.home, MODULE.PUBLIC_OWNER) / SHA_B
        )
        skill_file = release_dir / "personal_codex" / "skills" / "public-new" / "SKILL.md"
        skill_file.write_text("# Tampered\n", encoding="utf-8")
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending after-state release tree changed",
        ):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)
        self.assertTrue(self.added_target.is_symlink())

    def test_committed_same_tree_new_release_inode_blocks_pointer_clear(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        release_dir = (
            MODULE._releases_root(self.home, MODULE.PUBLIC_OWNER) / SHA_B
        )
        expected_tree, expected_directory_identity = (
            MODULE._installed_release_identity_and_directory_identity(
                self.home,
                MODULE.PUBLIC_OWNER,
                SHA_B,
            )
        )
        displaced = release_dir.with_name(f"{SHA_B}.displaced")
        release_dir.rename(displaced)
        shutil.copytree(displaced, release_dir)
        actual_tree, actual_directory_identity = (
            MODULE._installed_release_identity_and_directory_identity(
                self.home,
                MODULE.PUBLIC_OWNER,
                SHA_B,
            )
        )
        self.assertEqual(actual_tree, expected_tree)
        self.assertNotEqual(actual_directory_identity, expected_directory_identity)
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending after-state release tree changed",
        ):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)
        self.assertTrue(self.added_target.is_symlink())

    def test_legacy_v3_pending_metadata_fails_closed(self) -> None:
        batch, _actions, state, state_snapshot = self._stage_crash_batch()
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["version"] = 3
        for field in (
            "releases_before",
            "releases_after",
            "commit_evidence",
            "commit_marker",
        ):
            payload.pop(field)
        metadata_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported fields or version",
        ):
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(MODULE._current_sha(self.home), SHA_A)
        self.assertFalse(os.path.lexists(self.added_target))
        marker = batch.batch_root / Path(*batch.commit_marker_path.parts)
        self.assertFalse(os.path.lexists(marker))

    def test_committed_unchanged_managed_claim_inode_racer_blocks_finalize(
        self,
    ) -> None:
        batch, _state, state_snapshot = self._stage_unclaimed_removal_batch()
        self._publish_committed_state(batch, state_snapshot)
        target = self.home / "skills" / "public-base"
        link_target = os.readlink(target)
        target.unlink()
        target.symlink_to(link_target, target_is_directory=True)
        racer_identity = (target.lstat().st_dev, target.lstat().st_ino)
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(MODULE.SyncError, "after-state claim"):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            racer_identity,
        )

    def test_committed_unchanged_current_claim_inode_racer_blocks_finalize(
        self,
    ) -> None:
        batch, _state, state_snapshot = self._stage_unclaimed_removal_batch()
        self._publish_committed_state(batch, state_snapshot)
        current = MODULE._current_link(self.home)
        link_target = os.readlink(current)
        current.unlink()
        current.symlink_to(link_target, target_is_directory=True)
        racer_identity = (current.lstat().st_dev, current.lstat().st_ino)
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(MODULE.SyncError, "after-state claim"):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (current.lstat().st_dev, current.lstat().st_ino),
            racer_identity,
        )

    def test_uncommitted_unchanged_current_claim_inode_racer_blocks_clear(
        self,
    ) -> None:
        _batch, state, state_snapshot = self._stage_unclaimed_removal_batch()
        current = MODULE._current_link(self.home)
        link_target = os.readlink(current)
        current.unlink()
        current.symlink_to(link_target, target_is_directory=True)
        racer_identity = (current.lstat().st_dev, current.lstat().st_ino)

        with self.assertRaisesRegex(MODULE.SyncError, "before-state claim"):
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (current.lstat().st_dev, current.lstat().st_ino),
            racer_identity,
        )

    def test_state_directory_swap_cannot_hide_home_anchored_pointer(self) -> None:
        batch, actions, _state, _state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        state_parent = self.state_path.parent
        displaced = state_parent.with_name("state-displaced-after-pointer")
        payload = self.state_path.read_bytes()
        mode = self.state_path.stat().st_mode & 0o777
        parent_mode = state_parent.stat().st_mode & 0o777
        state_parent.rename(displaced)
        state_parent.mkdir(mode=parent_mode)
        self.state_path.write_bytes(payload)
        self.state_path.chmod(mode)
        foreign_state, foreign_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        self.assertIsNotNone(MODULE._load_pending_link_batch(self.home))
        with self.assertRaisesRegex(MODULE.SyncError, "ambiguous canonical"):
            MODULE._recover_pending_link_transaction(
                self.home,
                foreign_state,
                foreign_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())

    def test_sync_root_swap_cannot_hide_home_anchored_pointer(self) -> None:
        batch, actions, _state, _state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        sync_root = MODULE._personal_sync_root(self.home)
        displaced = self.home / "personal-sync-displaced-after-pointer"
        sync_root.rename(displaced)
        sync_root.mkdir()

        with self.assertRaises((OSError, MODULE.SyncError)):
            MODULE._recover_pending_link_transaction(
                self.home,
                MODULE._empty_managed_state(),
                MODULE.ManagedStateFileSnapshot(exists=False),
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())

    def test_missing_pointer_never_counts_as_successful_clear(self) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        self.pointer_path.unlink()

        with self.assertRaisesRegex(MODULE.SyncError, "pointer disappeared"):
            MODULE._clear_pending_link_pointer(self.home, batch, phase="before")

    def test_exact_noop_does_not_create_an_empty_pending_batch(self) -> None:
        quarantine = MODULE._personal_sync_root(self.home) / "quarantine"
        before = tuple(sorted(path.name for path in quarantine.iterdir()))

        install_quietly(self.release_a, self.home, SHA_A)

        after = tuple(sorted(path.name for path in quarantine.iterdir()))
        self.assertEqual(after, before)

    def test_committed_install_removes_only_its_ticketed_batch(self) -> None:
        unrelated_batch = MODULE._quarantine_batch_root(self.home, [])
        captured_batches: list[Path] = []
        real_stage = MODULE._stage_pending_link_batch

        def capture_stage(*args: object, **kwargs: object) -> MODULE.PendingLinkBatch:
            batch = real_stage(*args, **kwargs)
            captured_batches.append(batch.batch_root)
            return batch

        with mock.patch.object(
            MODULE,
            "_stage_pending_link_batch",
            side_effect=capture_stage,
        ):
            install_quietly(self.release_b, self.home, SHA_B)

        self.assertEqual(len(captured_batches), 1)
        self.assertFalse(os.path.lexists(captured_batches[0]))
        self.assertTrue(unrelated_batch.is_dir())
        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)

    def test_deferred_cleanup_resumes_without_following_symlinks(self) -> None:
        unrelated_batch = MODULE._quarantine_batch_root(self.home, [])
        batch_root, ticket_path, output = self._install_with_deferred_cleanup()

        self.assertIn("cleanup was deferred", output)
        self.assertTrue(batch_root.is_dir())
        self.assertTrue(ticket_path.is_file())
        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.install_release_tree(
                self.release_b,
                self.home,
                SHA_B,
                dry_run=True,
            )
        self.assertTrue(batch_root.is_dir())
        self.assertTrue(ticket_path.is_file())

        outside = self.root / "outside-cleanup"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep\n", encoding="utf-8")
        shutil.rmtree(batch_root / "pending")
        (batch_root / "pending").symlink_to(outside, target_is_directory=True)
        deep_root = batch_root / "deep" / Path(
            *(
                f"level-{index:02d}"
                for index in range(MODULE.MAX_MANIFEST_TARGET_PATH_DEPTH)
            )
        )
        deep_root.mkdir(parents=True)
        (deep_root / "leaf").write_text("cleanup\n", encoding="utf-8")

        install_quietly(self.release_b, self.home, SHA_B)

        self.assertFalse(os.path.lexists(batch_root))
        self.assertFalse(os.path.lexists(ticket_path))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(unrelated_batch.is_dir())

    def test_cleanup_ticket_survives_missing_quarantine_root(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        quarantine_root = batch_root.parent
        displaced = quarantine_root.with_name("quarantine-displaced")
        quarantine_root.rename(displaced)

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("quarantine root is missing", stdout.getvalue())
        self.assertTrue(ticket_path.is_file())
        displaced.rename(quarantine_root)
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(os.path.lexists(batch_root))
        self.assertFalse(os.path.lexists(ticket_path))

    def test_cleanup_rejects_special_nodes_and_resumes_after_repair(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        fifo = batch_root / "unsupported-fifo"
        os.mkfifo(fifo, mode=0o600)

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("unsupported entry", stdout.getvalue())
        self.assertTrue(batch_root.is_dir())
        self.assertTrue(ticket_path.is_file())
        fifo.unlink()
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(os.path.lexists(batch_root))
        self.assertFalse(os.path.lexists(ticket_path))

    def test_cleanup_file_racer_is_retained_across_retries(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        victim = batch_root / "cleanup-race-file"
        expected = batch_root / "cleanup-race-file-expected"
        victim.write_text("expected\n", encoding="utf-8")
        expected_identity = (victim.stat().st_dev, victim.stat().st_ino)
        foreign_identity: tuple[int, int] | None = None
        real_rename = MODULE._rename_noreplace_at

        def replace_before_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal foreign_identity
            if (
                source_name == victim.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
                and foreign_identity is None
            ):
                victim.rename(expected)
                victim.write_text("foreign\n", encoding="utf-8")
                foreign_identity = (victim.stat().st_dev, victim.stat().st_ino)
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
                side_effect=replace_before_isolation,
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("preserved as", stdout.getvalue())
        retained = list(
            batch_root.glob(f"{MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX}*")
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(encoding="utf-8"), "foreign\n")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            foreign_identity,
        )
        self.assertEqual(expected.read_text(encoding="utf-8"), "expected\n")
        self.assertEqual(
            (expected.stat().st_dev, expected.stat().st_ino),
            expected_identity,
        )

        with contextlib.redirect_stdout(io.StringIO()) as retry_stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)
        self.assertIn("requires manual cleanup", retry_stdout.getvalue())
        self.assertTrue(ticket_path.is_file())
        self.assertEqual(retained[0].read_text(encoding="utf-8"), "foreign\n")
        self.assertEqual(expected.read_text(encoding="utf-8"), "expected\n")

    def test_cleanup_directory_racer_is_retained_across_retries(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        victim = batch_root / "cleanup-race-directory"
        expected = batch_root / "cleanup-race-directory-expected"
        victim.mkdir()
        (victim / "sentinel").write_text("expected\n", encoding="utf-8")
        expected_identity = (victim.stat().st_dev, victim.stat().st_ino)
        foreign_identity: tuple[int, int] | None = None
        real_rename = MODULE._rename_noreplace_at

        def replace_before_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal foreign_identity
            if (
                source_name == victim.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
                and foreign_identity is None
            ):
                victim.rename(expected)
                victim.mkdir()
                (victim / "sentinel").write_text("foreign\n", encoding="utf-8")
                foreign_identity = (victim.stat().st_dev, victim.stat().st_ino)
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
                side_effect=replace_before_isolation,
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("preserved as", stdout.getvalue())
        retained = list(
            batch_root.glob(f"{MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX}*")
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            (retained[0] / "sentinel").read_text(encoding="utf-8"),
            "foreign\n",
        )
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            foreign_identity,
        )
        self.assertEqual(
            (expected / "sentinel").read_text(encoding="utf-8"),
            "expected\n",
        )
        self.assertEqual(
            (expected.stat().st_dev, expected.stat().st_ino),
            expected_identity,
        )

        with contextlib.redirect_stdout(io.StringIO()) as retry_stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)
        self.assertIn("requires manual cleanup", retry_stdout.getvalue())
        self.assertTrue(ticket_path.is_file())
        self.assertEqual(
            (retained[0] / "sentinel").read_text(encoding="utf-8"),
            "foreign\n",
        )

    def test_cleanup_resumes_after_active_entry_isolation_interruption(
        self,
    ) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        victim = batch_root / "cleanup-interrupted-file"
        victim.write_text("cleanup\n", encoding="utf-8")
        real_rename = MODULE._rename_noreplace_at
        interrupted = False

        def interrupt_after_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal interrupted
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                source_name == victim.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
                and not interrupted
            ):
                interrupted = True
                raise OSError("injected post-isolation interruption")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=interrupt_after_isolation,
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertTrue(interrupted)
        self.assertIn("post-isolation interruption", stdout.getvalue())
        active = list(
            batch_root.glob(f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}*")
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].read_text(encoding="utf-8"), "cleanup\n")

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(os.path.lexists(batch_root))
        self.assertFalse(os.path.lexists(ticket_path))

    def test_cleanup_retry_reisolates_active_entry_before_deletion(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        victim = batch_root / "cleanup-retry-file"
        victim.write_text("expected\n", encoding="utf-8")
        expected_identity = (victim.stat().st_dev, victim.stat().st_ino)
        real_rename = MODULE._rename_noreplace_at
        interrupted = False

        def interrupt_after_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal interrupted
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                source_name == victim.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
                and not interrupted
            ):
                interrupted = True
                raise OSError("injected post-isolation interruption")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=interrupt_after_isolation,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        active = list(
            batch_root.glob(f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}*")
        )
        self.assertEqual(len(active), 1)
        expected = self.root / "cleanup-retry-file-expected"
        foreign_identity: tuple[int, int] | None = None

        def replace_before_reisolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal foreign_identity
            if (
                source_name == active[0].name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
                and foreign_identity is None
            ):
                active[0].rename(expected)
                replacement = batch_root / source_name
                replacement.write_text("foreign\n", encoding="utf-8")
                foreign_identity = (
                    replacement.stat().st_dev,
                    replacement.stat().st_ino,
                )
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
                side_effect=replace_before_reisolation,
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("preserved as", stdout.getvalue())
        retained = list(
            batch_root.glob(f"{MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX}*")
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(encoding="utf-8"), "foreign\n")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            foreign_identity,
        )
        self.assertEqual(expected.read_text(encoding="utf-8"), "expected\n")
        self.assertEqual(
            (expected.stat().st_dev, expected.stat().st_ino),
            expected_identity,
        )
        self.assertTrue(ticket_path.is_file())

    def test_cleanup_resumes_mismatched_active_entry_as_retained(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        victim = batch_root / "cleanup-mismatched-active-file"
        expected = self.root / "cleanup-mismatched-active-file-expected"
        victim.write_text("expected\n", encoding="utf-8")
        expected_identity = (victim.stat().st_dev, victim.stat().st_ino)
        foreign_identity: tuple[int, int] | None = None
        real_rename = MODULE._rename_noreplace_at
        interrupted = False

        def interrupt_after_mismatch_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal foreign_identity, interrupted
            if (
                source_name == victim.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX
                )
                and not interrupted
            ):
                victim.rename(expected)
                victim.write_text("foreign\n", encoding="utf-8")
                foreign_identity = (victim.stat().st_dev, victim.stat().st_ino)
                real_rename(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )
                interrupted = True
                raise OSError("injected mismatched-isolation interruption")
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
                side_effect=interrupt_after_mismatch_isolation,
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("mismatched-isolation interruption", stdout.getvalue())
        active = list(
            batch_root.glob(f"{MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX}*")
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].read_text(encoding="utf-8"), "foreign\n")

        with contextlib.redirect_stdout(io.StringIO()) as retry_stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("preserved as", retry_stdout.getvalue())
        retained = list(
            batch_root.glob(f"{MODULE.PENDING_CLEANUP_RETAINED_ENTRY_PREFIX}*")
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(encoding="utf-8"), "foreign\n")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            foreign_identity,
        )
        self.assertEqual(expected.read_text(encoding="utf-8"), "expected\n")
        self.assertEqual(
            (expected.stat().st_dev, expected.stat().st_ino),
            expected_identity,
        )
        self.assertTrue(ticket_path.is_file())

    def test_committed_recovery_removes_batch_and_external_ticket(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch.batch_root.name,
        )

        MODULE._recover_pending_link_transaction(
            self.home,
            committed_state,
            committed_snapshot,
            dry_run=False,
        )

        self.assertFalse(os.path.lexists(batch.batch_root))
        self.assertFalse(os.path.lexists(ticket_path))
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_cleanup_ticket_recovers_from_truncated_temp_publication(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            MODULE._pending_cleanup_index_path(self.home),
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch.batch_root.name,
        )
        temp_path = ticket_path.with_name(ticket_path.name + ".tmp")
        temp_path.write_bytes(b"{")
        temp_path.chmod(0o600)

        MODULE._mark_pending_batch_cleanup_ready(self.home, batch)

        self.assertFalse(os.path.lexists(temp_path))
        self.assertIsNotNone(
            MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        )
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        MODULE._recover_pending_link_transaction(
            self.home,
            committed_state,
            committed_snapshot,
            dry_run=False,
        )
        self.assertFalse(os.path.lexists(batch.batch_root))
        self.assertFalse(os.path.lexists(ticket_path))

    def test_truncated_cleanup_temp_racer_is_retained_before_deletion(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        temp_path = index_root / "orphan.json.tmp"
        expected_path = index_root / "expected-orphan-temp"
        temp_path.write_bytes(b"{")
        temp_path.chmod(0o600)
        real_rename = MODULE._rename_noreplace_at
        foreign_identity: tuple[int, int] | None = None

        def replace_before_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal foreign_identity
            if (
                source_name == temp_path.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_RETAINED_PREFIX
                )
                and foreign_identity is None
            ):
                temp_path.rename(expected_path)
                temp_path.write_bytes(b"foreign-temp\n")
                temp_path.chmod(0o600)
                metadata = temp_path.stat()
                foreign_identity = (metadata.st_dev, metadata.st_ino)
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
                side_effect=replace_before_isolation,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "preserved as"),
        ):
            MODULE._discard_incomplete_pending_cleanup_ticket(
                self.home,
                temp_path,
            )

        retained = list(
            index_root.glob(f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}*")
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"foreign-temp\n")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            foreign_identity,
        )
        self.assertEqual(expected_path.read_bytes(), b"{")

    def test_cleanup_temp_publication_racer_is_retained(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_path = index_root / "20260716T000000Z-1-1.json"
        temp_path = ticket_path.with_name(ticket_path.name + ".tmp")
        expected_path = temp_path.with_name(temp_path.name + ".expected")
        payload = b'{"version":1}\n'
        ticket_path.write_bytes(payload)
        ticket_path.chmod(0o600)
        real_rename = MODULE._rename_noreplace_at
        foreign_identity: tuple[int, int] | None = None

        def replace_publication_temp(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal foreign_identity
            if (
                source_name == temp_path.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_RETAINED_PREFIX
                )
                and foreign_identity is None
            ):
                temp_path.rename(expected_path)
                temp_path.write_bytes(b"foreign-publication-temp\n")
                temp_path.chmod(0o600)
                metadata = temp_path.stat()
                foreign_identity = (metadata.st_dev, metadata.st_ino)
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
                side_effect=replace_publication_temp,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "preserved as"),
        ):
            MODULE._publish_pending_cleanup_ticket(
                self.home,
                ticket_path,
                payload,
            )

        retained = list(
            index_root.glob(f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}*")
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"foreign-publication-temp\n")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            foreign_identity,
        )
        self.assertEqual(ticket_path.read_bytes(), payload)
        self.assertEqual(expected_path.read_bytes(), payload)

    def test_cleanup_ticket_racer_is_retained_before_deletion(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        expected_path = ticket_path.with_name(ticket_path.name + ".expected")
        real_rename = MODULE._rename_noreplace_at
        foreign_identity: tuple[int, int] | None = None

        def replace_ticket_before_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal foreign_identity
            if (
                source_name == ticket_path.name
                and destination_name.startswith(
                    MODULE.PENDING_CLEANUP_RETAINED_PREFIX
                )
                and foreign_identity is None
            ):
                ticket_path.rename(expected_path)
                ticket_path.write_bytes(b"foreign-ticket\n")
                ticket_path.chmod(0o600)
                metadata = ticket_path.stat()
                foreign_identity = (metadata.st_dev, metadata.st_ino)
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
                side_effect=replace_ticket_before_isolation,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "preserved as"),
        ):
            MODULE._delete_pending_cleanup_ticket(self.home, ticket)

        retained = list(
            ticket_path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}*"
            )
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"foreign-ticket\n")
        self.assertEqual(
            (retained[0].stat().st_dev, retained[0].stat().st_ino),
            foreign_identity,
        )
        self.assertEqual(expected_path.read_bytes(), ticket.snapshot.payload)
        self.assertTrue(batch_root.is_dir())

    def test_cleanup_batch_root_racer_is_isolated_and_recoverable(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        displaced = batch_root.with_name(batch_root.name + "-expected")
        isolated = batch_root.with_name(
            MODULE._pending_cleanup_isolated_batch_name(batch_root.name)
        )
        real_rename = MODULE._rename_noreplace_at
        raced = False

        def replace_batch_before_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal raced
            if (
                source_name == batch_root.name
                and destination_name == isolated.name
                and not raced
            ):
                raced = True
                batch_root.rename(displaced)
                batch_root.mkdir(mode=0o700)
                (batch_root / "sentinel").write_text(
                    "foreign-batch\n",
                    encoding="utf-8",
                )
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
                side_effect=replace_batch_before_isolation,
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("batch root changed", stdout.getvalue())
        self.assertTrue(ticket_path.is_file())
        self.assertEqual(
            (isolated / "sentinel").read_text(encoding="utf-8"),
            "foreign-batch\n",
        )
        foreign = batch_root.with_name(batch_root.name + "-foreign")
        isolated.rename(foreign)
        displaced.rename(batch_root)

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(os.path.lexists(batch_root))
        self.assertFalse(os.path.lexists(ticket_path))
        self.assertEqual(
            (foreign / "sentinel").read_text(encoding="utf-8"),
            "foreign-batch\n",
        )

    def test_cleanup_resumes_from_isolated_batch_after_rmdir_failure(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        isolated_name = MODULE._pending_cleanup_isolated_batch_name(
            batch_root.name
        )
        isolated = batch_root.with_name(isolated_name)
        real_rmdir = os.rmdir

        def fail_isolated_rmdir(
            name: str | bytes,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if name == isolated_name:
                raise OSError("injected isolated batch rmdir failure")
            real_rmdir(name, dir_fd=dir_fd)

        with (
            mock.patch.object(os, "rmdir", side_effect=fail_isolated_rmdir),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("isolated batch rmdir failure", stdout.getvalue())
        self.assertFalse(os.path.lexists(batch_root))
        self.assertTrue(isolated.is_dir())
        self.assertTrue(ticket_path.is_file())
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(os.path.lexists(isolated))
        self.assertFalse(os.path.lexists(ticket_path))

    def test_existing_cleanup_ticket_is_fsynced_before_pointer_clear(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch.batch_root.name,
        )

        with (
            mock.patch.object(
                MODULE,
                "_verify_pending_cleanup_ticket_durable",
                side_effect=MODULE.SyncError("injected index fsync failure"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "index fsync failure"),
        ):
            MODULE._mark_pending_batch_cleanup_ready(self.home, batch)

        self.assertTrue(ticket_path.is_file())
        self.assertTrue(self.pointer_path.is_file())
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        events: list[str] = []
        real_durable = MODULE._verify_pending_cleanup_ticket_durable
        real_clear = MODULE._clear_pending_link_pointer

        def verify_durable(*args: object, **kwargs: object) -> None:
            events.append("durable")
            real_durable(*args, **kwargs)

        def clear_pointer(*args: object, **kwargs: object) -> None:
            events.append("clear")
            real_clear(*args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_verify_pending_cleanup_ticket_durable",
                side_effect=verify_durable,
            ),
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=clear_pointer,
            ),
        ):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertEqual(events[:2], ["durable", "clear"])
        self.assertFalse(os.path.lexists(batch.batch_root))
        self.assertFalse(os.path.lexists(ticket_path))

    def test_deleted_batch_with_retained_ticket_finishes_on_retry(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        real_delete = MODULE._delete_pending_cleanup_ticket
        failed = False

        def fail_once(*args: object, **kwargs: object) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise MODULE.SyncError("injected ticket unlink failure")
            real_delete(*args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_delete_pending_cleanup_ticket",
                side_effect=fail_once,
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("ticket unlink failure", stdout.getvalue())
        self.assertFalse(os.path.lexists(batch_root))
        self.assertTrue(ticket_path.is_file())
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(os.path.lexists(ticket_path))

    def test_cleanup_rejects_replaced_batch_root_identity(self) -> None:
        batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        displaced = batch_root.with_name(batch_root.name + "-displaced")
        batch_root.rename(displaced)
        batch_root.mkdir(mode=0o700)
        sentinel = batch_root / "sentinel"
        sentinel.write_text("keep\n", encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("batch root changed", stdout.getvalue())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(ticket_path.is_file())
        replacement = batch_root.with_name(batch_root.name + "-replacement")
        batch_root.rename(replacement)
        displaced.rename(batch_root)
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
        self.assertFalse(os.path.lexists(batch_root))
        self.assertTrue(replacement.is_dir())

    def test_cleanup_cursor_rotates_past_failed_tickets(self) -> None:
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            MODULE._pending_cleanup_index_path(self.home),
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        quarantine_root = MODULE._personal_sync_root(self.home) / "quarantine"
        ticket_names: list[str] = []
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN + 2):
            batch_name = f"20260716T000000Z-1-{index:02d}"
            batch_root = quarantine_root / batch_name
            batch_root.mkdir(mode=0o700)
            metadata = batch_root.stat()
            ticket_path = MODULE._pending_cleanup_ticket_path(
                self.home,
                batch_name,
            )
            payload = MODULE._pending_cleanup_ticket_payload(
                batch_root,
                (metadata.st_dev, metadata.st_ino),
                (1, 1),
                (1, 2),
                0o600,
                "a" * 64,
            )
            MODULE._publish_pending_cleanup_ticket(
                self.home,
                ticket_path,
                payload,
            )
            ticket_names.append(ticket_path.name)

        attempts: list[str] = []

        def retain(_home: Path, ticket: MODULE.PendingBatchCleanupTicket) -> bool:
            attempts.append(ticket.path.name)
            return False

        with mock.patch.object(
            MODULE,
            "_remove_cleanup_ready_batch",
            side_effect=retain,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)
            first_attempts = tuple(attempts)
            attempts.clear()
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)
            second_attempts = tuple(attempts)

        self.assertEqual(
            first_attempts,
            tuple(ticket_names[: MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN]),
        )
        self.assertIn(ticket_names[-2], second_attempts)
        self.assertIn(ticket_names[-1], second_attempts)

    def test_cleanup_refuses_child_mount_identity_change(self) -> None:
        cleanup_root = self.root / "cleanup-mount-root"
        child = cleanup_root / "child"
        child.mkdir(parents=True)
        sentinel = child / "sentinel"
        sentinel.write_text("keep\n", encoding="utf-8")
        root_fd = os.open(
            cleanup_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            root_identity = MODULE._directory_identity(root_fd)
            root_mount = (root_identity[0], 100)

            def mount_identity(directory_fd: int) -> tuple[int, int]:
                if directory_fd == root_fd:
                    return root_mount
                return root_identity[0], 101

            with (
                mock.patch.object(
                    MODULE,
                    "_directory_mount_identity",
                    side_effect=mount_identity,
                ),
                self.assertRaisesRegex(MODULE.SyncError, "mount boundary"),
            ):
                MODULE._remove_pending_batch_directory_contents(
                    root_fd,
                    root_identity,
                    root_mount,
                    [100],
                    depth=0,
                )
        finally:
            os.close(root_fd)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_cleanup_durability_parent_open_failure_closes_index_fd(self) -> None:
        _batch_root, ticket_path, _output = self._install_with_deferred_cleanup()
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        real_open = MODULE._open_directory_beneath
        opened_index_fd: int | None = None

        def fail_parent_open(home: Path, path: Path) -> int:
            nonlocal opened_index_fd
            if path == ticket.path.parent:
                opened_index_fd = real_open(home, path)
                return opened_index_fd
            if path == ticket.path.parent.parent:
                raise MODULE.SyncError("injected cleanup index parent failure")
            return real_open(home, path)

        with (
            mock.patch.object(
                MODULE,
                "_open_directory_beneath",
                side_effect=fail_parent_open,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "index parent failure"),
        ):
            MODULE._verify_pending_cleanup_ticket_durable(self.home, ticket)

        self.assertIsNotNone(opened_index_fd)
        assert opened_index_fd is not None
        with self.assertRaises(OSError):
            os.fstat(opened_index_fd)

    def test_first_install_missing_parent_commit_recovers_automatically(self) -> None:
        first_home = self.root / "first-install-home"
        first_pointer = MODULE._pending_link_pointer_path(first_home)

        with mock.patch.object(
            MODULE,
            "_clear_pending_link_pointer",
            side_effect=MODULE.SyncError("injected first-install clear failure"),
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                install_quietly(self.release_a, first_home, SHA_A)

        self.assertTrue(first_pointer.is_file())
        batch = MODULE._load_pending_link_batch(first_home)
        self.assertIsNotNone(batch)
        assert batch is not None
        managed_record = next(
            record for record in batch.records if record.scope == "managed"
        )
        self.assertIsNotNone(managed_record.planned_snapshot.parent_identity)
        self.assertFalse(managed_record.planned_snapshot.missing_parent_parts)

        install_quietly(self.release_a, first_home, SHA_A)

        self.assertFalse(os.path.lexists(first_pointer))
        self.assertEqual(MODULE._current_sha(first_home), SHA_A)
        self.assertTrue((first_home / "skills" / "public-base").is_symlink())

    def test_public_install_preflight_defers_and_recovers_published_target(self) -> None:
        batch, actions, _state, _state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        first_identity = (
            self.added_target.lstat().st_dev,
            self.added_target.lstat().st_ino,
        )

        install_quietly(self.release_b, self.home, SHA_B)

        final_state = MODULE._load_managed_state(self.home)
        self.assertIn(PurePosixPath("skills/added"), final_state.links)
        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertNotEqual(
            (
                self.added_target.lstat().st_dev,
                self.added_target.lstat().st_ino,
            ),
            first_identity,
        )

    def test_uncommitted_same_target_different_inode_racer_blocks_recovery(
        self,
    ) -> None:
        batch, actions, state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        link_target = os.readlink(self.added_target)
        self.added_target.unlink()
        self.added_target.symlink_to(link_target, target_is_directory=True)
        racer_identity = (
            self.added_target.lstat().st_dev,
            self.added_target.lstat().st_ino,
        )

        with self.assertRaisesRegex(MODULE.SyncError, "create absence changed"):
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )

        self.assertEqual(
            (
                self.added_target.lstat().st_dev,
                self.added_target.lstat().st_ino,
            ),
            racer_identity,
        )
        self.assertTrue(self.pointer_path.is_file())

    def test_committed_same_target_different_inode_racer_blocks_finalize(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        link_target = os.readlink(self.added_target)
        self.added_target.unlink()
        self.added_target.symlink_to(link_target, target_is_directory=True)
        racer_identity = (
            self.added_target.lstat().st_dev,
            self.added_target.lstat().st_ino,
        )
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(MODULE.SyncError, "exact evidence inode"):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (
                self.added_target.lstat().st_dev,
                self.added_target.lstat().st_ino,
            ),
            racer_identity,
        )

    def test_committed_current_replace_requires_exact_evidence_inode(self) -> None:
        batch, actions, _state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        self._publish_committed_state(batch, state_snapshot)
        current = MODULE._current_link(self.home)
        link_target = os.readlink(current)
        current.unlink()
        current.symlink_to(link_target, target_is_directory=True)
        racer_identity = (current.lstat().st_dev, current.lstat().st_ino)
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(MODULE.SyncError, "exact evidence inode"):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (current.lstat().st_dev, current.lstat().st_ino),
            racer_identity,
        )

    def test_before_state_parent_swap_with_exact_leaf_retains_pointer(self) -> None:
        batch, state, state_snapshot = self._stage_single_added_create_batch()
        evidence_identity = self._move_exact_added_inode_under_replaced_parent(
            batch
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "target parent changed|managed target changed after planning",
        ):
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (self.added_target.lstat().st_dev, self.added_target.lstat().st_ino),
            evidence_identity,
        )

    def test_committed_parent_swap_with_exact_leaf_retains_pointer(self) -> None:
        batch, _state, state_snapshot = self._stage_single_added_create_batch()
        self._publish_committed_state(batch, state_snapshot)
        evidence_identity = self._move_exact_added_inode_under_replaced_parent(
            batch
        )
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        with self.assertRaisesRegex(MODULE.SyncError, "target parent changed"):
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (self.added_target.lstat().st_dev, self.added_target.lstat().st_ino),
            evidence_identity,
        )

    def test_missing_canonical_state_restores_exact_before_and_rolls_back_link(
        self,
    ) -> None:
        batch, actions, state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        state_transaction = MODULE._prepare_pending_managed_state_transaction(
            self.home,
            batch,
            batch.state_after_value,
        )
        state_parent_fd = MODULE._open_directory_beneath(
            self.home,
            self.state_path.parent,
        )
        try:
            state_transaction.state_parent_identity = MODULE._directory_identity(
                state_parent_fd
            )
            backup, matches = MODULE._move_managed_state_entry_to_quarantine(
                self.home,
                state_transaction,
                state_parent_fd,
                self.state_path.name,
                "original",
                destination_name=self.state_path.name,
                expected_identity=state_snapshot.file_identity,
                expected_snapshot=state_snapshot,
            )
            state_transaction.backup = backup
            self.assertTrue(matches)
        finally:
            MODULE._close_fd_quietly(state_parent_fd)
        self.assertFalse(os.path.lexists(self.state_path))
        missing_state, missing_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )

        recovered, recovered_snapshot, did_recover = (
            MODULE._recover_pending_link_transaction(
                self.home,
                missing_state,
                missing_snapshot,
                dry_run=False,
            )
        )

        self.assertTrue(did_recover)
        self.assertTrue(recovered_snapshot.exists)
        self.assertEqual(recovered, state)
        self.assertFalse(os.path.lexists(self.added_target))
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_quarantine_reverse_rename_fsyncs_both_directories(self) -> None:
        batch, _actions, _state, state_snapshot = self._stage_crash_batch()
        transaction = MODULE._prepare_pending_managed_state_transaction(
            self.home,
            batch,
            batch.state_after_value,
        )
        state_parent_fd = MODULE._open_directory_beneath(
            self.home,
            self.state_path.parent,
        )
        events: list[tuple[str, int, int | None]] = []
        real_rename = MODULE._rename_noreplace_at

        def trace_rename(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            events.append(("rename", source_parent_fd, destination_parent_fd))
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        def trace_fsync(file_descriptor: int) -> None:
            events.append(("fsync", file_descriptor, None))

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=trace_rename,
                ),
                mock.patch.object(MODULE.os, "fsync", side_effect=trace_fsync),
                mock.patch.object(
                    MODULE,
                    "_managed_state_file_matches",
                    return_value=False,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "changed during quarantine",
                ),
            ):
                MODULE._move_managed_state_entry_to_quarantine(
                    self.home,
                    transaction,
                    state_parent_fd,
                    self.state_path.name,
                    "forced-mismatch",
                    expected_identity=state_snapshot.file_identity,
                    expected_snapshot=state_snapshot,
                )
        finally:
            MODULE._close_fd_quietly(state_parent_fd)

        rename_indexes = [
            index
            for index, event in enumerate(events)
            if event[0] == "rename"
        ]
        self.assertEqual(len(rename_indexes), 2)
        reverse_event = events[rename_indexes[1]]
        fsynced_after_reverse = {
            event[1]
            for event in events[rename_indexes[1] + 1 :]
            if event[0] == "fsync"
        }
        self.assertEqual(
            fsynced_after_reverse,
            {reverse_event[1], reverse_event[2]},
        )
        self.assertEqual(
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
            state_snapshot.file_identity,
        )

    def test_pending_metadata_rejects_noncanonical_stage_path(self) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        MODULE._clear_pending_link_pointer(self.home, batch)
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["records"][0]["stage"] = "../escape"
        metadata_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        batch.pointer_snapshot = None
        MODULE._publish_pending_link_pointer(self.home, batch)

        with self.assertRaisesRegex(MODULE.SyncError, "pending stage"):
            MODULE._load_pending_link_batch(self.home)

    def test_pending_metadata_requires_exact_after_claim_set(self) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        MODULE._clear_pending_link_pointer(self.home, batch, phase="before")
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["claims_after"].pop()
        metadata_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        batch.pointer_snapshot = None
        MODULE._publish_pending_link_pointer(self.home, batch)

        with self.assertRaisesRegex(MODULE.SyncError, "exactly match state"):
            MODULE._load_pending_link_batch(self.home)

    def test_pending_claim_cap_boundary_is_parseable(self) -> None:
        with mock.patch.object(MODULE, "MAX_PENDING_LINK_CLAIMS", 3):
            batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
            parsed = MODULE._load_pending_link_batch(self.home)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(len(batch.claims_after), 3)
        self.assertEqual(len(parsed.claims_after), 3)

    def test_pending_claim_cap_rejects_stage_before_pointer_publication(self) -> None:
        quarantine = MODULE._personal_sync_root(self.home) / "quarantine"
        before_batches = tuple(sorted(path.name for path in quarantine.iterdir()))

        with mock.patch.object(MODULE, "MAX_PENDING_LINK_CLAIMS", 2):
            with self.assertRaisesRegex(MODULE.SyncError, "after-state claims"):
                self._stage_crash_batch()

        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertEqual(
            tuple(sorted(path.name for path in quarantine.iterdir())),
            before_batches,
        )

    def test_state_claimed_create_foreign_racer_blocks_before_clear(self) -> None:
        desired, actions, state, state_snapshot, next_state = (
            self._missing_managed_repair_inputs()
        )
        batch = MODULE._stage_pending_link_batch(
            self.home,
            [("managed", actions)],
            desired,
            state.owners,
            state_snapshot,
            state,
            next_state,
        )
        self.assertNotIn(
            ("managed", PurePosixPath("skills/public-base")),
            {(claim.scope, claim.target) for claim in batch.claims_before},
        )
        MODULE._publish_pending_link_pointer(self.home, batch)
        target = self.home / "skills" / "public-base"
        target.symlink_to("foreign", target_is_directory=True)
        racer_identity = (target.lstat().st_dev, target.lstat().st_ino)

        with self.assertRaisesRegex(MODULE.SyncError, "create absence changed"):
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(os.readlink(target), "foreign")
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            racer_identity,
        )

    def test_state_claimed_create_same_target_racer_blocks_staging(self) -> None:
        desired, actions, state, state_snapshot, next_state = (
            self._missing_managed_repair_inputs()
        )
        target = self.home / "skills" / "public-base"
        target.symlink_to(actions[0].link_target, target_is_directory=True)
        racer_identity = (target.lstat().st_dev, target.lstat().st_ino)

        with self.assertRaisesRegex(MODULE.SyncError, "create absence changed"):
            MODULE._stage_pending_link_batch(
                self.home,
                [("managed", actions)],
                desired,
                state.owners,
                state_snapshot,
                state,
                next_state,
            )

        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertEqual(os.readlink(target), actions[0].link_target)
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            racer_identity,
        )

    def test_state_claimed_create_foreign_racer_blocks_staging(self) -> None:
        desired, actions, state, state_snapshot, next_state = (
            self._missing_managed_repair_inputs()
        )
        target = self.home / "skills" / "public-base"
        target.symlink_to("foreign", target_is_directory=True)
        racer_identity = (target.lstat().st_dev, target.lstat().st_ino)

        with self.assertRaisesRegex(MODULE.SyncError, "create absence changed"):
            MODULE._stage_pending_link_batch(
                self.home,
                [("managed", actions)],
                desired,
                state.owners,
                state_snapshot,
                state,
                next_state,
            )

        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertEqual(os.readlink(target), "foreign")
        self.assertEqual(
            (target.lstat().st_dev, target.lstat().st_ino),
            racer_identity,
        )

    def test_legacy_state_mode_pending_batch_parses_and_recovers(self) -> None:
        self.state_path.chmod(0o644)

        _batch, _actions, state, state_snapshot = self._stage_crash_batch()
        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        recovered, _snapshot, did_recover = (
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )
        )

        self.assertTrue(did_recover)
        self.assertEqual(recovered, state)
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o644)
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_pending_parser_rejects_noncanonical_after_state_mode(self) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        MODULE._clear_pending_link_pointer(self.home, batch, phase="before")
        after_evidence = batch.batch_root / Path(*batch.state_after_evidence.parts)
        after_evidence.chmod(0o644)
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["state_after"]["mode"] = 0o644
        metadata_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        batch.pointer_snapshot = None
        MODULE._publish_pending_link_pointer(self.home, batch)

        with self.assertRaisesRegex(MODULE.SyncError, "file metadata is invalid"):
            MODULE._load_pending_link_batch(self.home)

    def test_pending_metadata_rejects_noncanonical_state_evidence_path(
        self,
    ) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        MODULE._clear_pending_link_pointer(self.home, batch)
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        after_evidence = batch.batch_root / Path(
            *batch.state_after_evidence.parts
        )
        alias = after_evidence.with_name("after-alias")
        os.link(after_evidence, alias)
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["state_after"]["evidence"] = "pending/state/after-alias"
        metadata_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        batch.pointer_snapshot = None
        MODULE._publish_pending_link_pointer(self.home, batch)

        with self.assertRaisesRegex(MODULE.SyncError, "path is not canonical"):
            MODULE._load_pending_link_batch(self.home)

    def test_pending_metadata_rejects_same_inode_state_phase_substitution(
        self,
    ) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        MODULE._clear_pending_link_pointer(self.home, batch)
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        before_evidence = batch.batch_root / Path(
            *MODULE.PENDING_STATE_BEFORE_EVIDENCE.parts
        )
        after_evidence = batch.batch_root / Path(
            *MODULE.PENDING_STATE_AFTER_EVIDENCE.parts
        )
        before_evidence.unlink()
        os.link(after_evidence, before_evidence)
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["state_before"] = dict(payload["state_after"])
        payload["state_before"]["evidence"] = "pending/state/before"
        metadata_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        batch.pointer_snapshot = None
        MODULE._publish_pending_link_pointer(self.home, batch)

        with self.assertRaisesRegex(MODULE.SyncError, "identities must differ"):
            MODULE._load_pending_link_batch(self.home)

    def test_pending_parser_builds_each_release_entry_index_once(self) -> None:
        self._stage_crash_batch()
        iterations = 0

        class CountingEntries(list[MODULE.LinkEntry]):
            def __iter__(self):  # type: ignore[no-untyped-def]
                nonlocal iterations
                iterations += 1
                return super().__iter__()

        counting_entries = CountingEntries(self.manifest_b.entries)
        counting_manifest = MODULE.ManifestData(
            owner=self.manifest_b.owner,
            entries=counting_entries,
            removed_links=self.manifest_b.removed_links,
            payload_digest=self.manifest_b.payload_digest,
            base_release_repo=self.manifest_b.base_release_repo,
            base_release_sha=self.manifest_b.base_release_sha,
        )
        real_load = MODULE._load_installed_manifest_data

        def load_counting_manifest(
            home: Path,
            owner: str,
            sha: str,
        ) -> MODULE.ManifestData:
            if owner == MODULE.PUBLIC_OWNER and sha == SHA_B:
                return counting_manifest
            return real_load(home, owner, sha)

        with mock.patch.object(
            MODULE,
            "_load_installed_manifest_data",
            side_effect=load_counting_manifest,
        ):
            parsed = MODULE._load_pending_link_batch(self.home)

        self.assertIsNotNone(parsed)
        self.assertEqual(iterations, 1)

    def test_failed_target_publication_preserves_same_target_new_inode_racer(
        self,
    ) -> None:
        batch, actions, _state, _state_snapshot = self._stage_crash_batch()
        action = next(action for action in actions if action.action == "create")
        record = next(
            record
            for record in batch.records
            if record.target == PurePosixPath("skills/added")
        )
        stage = batch.batch_root / Path(*record.stage.parts)
        stage_snapshot = MODULE._read_symlink_snapshot_beneath(self.home, stage)
        real_matches = MODULE._bound_directory_matches
        swapped = False
        racer_identity: tuple[int, int] | None = None

        def swap_after_publish(home: Path, directory: Path, directory_fd: int) -> bool:
            nonlocal swapped, racer_identity
            if (
                not swapped
                and directory == self.added_target.parent
                and os.path.lexists(self.added_target)
            ):
                swapped = True
                link_target = os.readlink(self.added_target)
                self.added_target.unlink()
                self.added_target.symlink_to(link_target, target_is_directory=True)
                racer_identity = (
                    self.added_target.lstat().st_dev,
                    self.added_target.lstat().st_ino,
                )
                return False
            return real_matches(home, directory, directory_fd)

        with mock.patch.object(
            MODULE,
            "_bound_directory_matches",
            side_effect=swap_after_publish,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "cleanup was incomplete"):
                MODULE._publish_symlink_hardlink_beneath(
                    self.home,
                    stage,
                    self.added_target,
                    stage_snapshot,
                    action.planned_snapshot,
                    {},
                )

        self.assertTrue(swapped)
        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (
                self.added_target.lstat().st_dev,
                self.added_target.lstat().st_ino,
            ),
            racer_identity,
        )

    def test_failed_pointer_publication_preserves_same_content_new_inode_racer(
        self,
    ) -> None:
        binding = MODULE._stage_release_tree_for_install(
            self.release_b,
            self.home,
            SHA_B,
            self.manifest_b,
        )
        MODULE._close_install_release_bindings([binding])
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
        current_manifest = MODULE._current_manifest_data(self.home, MODULE.PUBLIC_OWNER)
        actions = MODULE._plan_reconciliation(
            self.home,
            self.manifest_b.entries,
            current_manifest.entries,
            [],
            state,
            allow_cross_owner=False,
        )
        current_action = MODULE._plan_current_switch_action(
            self.home,
            SHA_B,
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
            self.manifest_b.entries,
            {MODULE.PUBLIC_OWNER: SHA_B},
            managed_targets,
        )
        batch = MODULE._stage_pending_link_batch(
            self.home,
            [("current", [current_action]), ("managed", actions)],
            self.manifest_b.entries,
            {MODULE.PUBLIC_OWNER: SHA_B},
            state_snapshot,
            state,
            next_state,
        )
        real_read = MODULE._read_managed_state_file_snapshot
        displaced: Path | None = None
        racer_identity: tuple[int, int] | None = None

        def swap_pointer_before_bound_read(
            home: Path,
            path: Path,
            parent_fd: int,
            *,
            expected_identity: tuple[int, int] | None = None,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal displaced, racer_identity
            if path == self.pointer_path and expected_identity is not None and displaced is None:
                payload = self.pointer_path.read_bytes()
                displaced = self.pointer_path.with_name("pending-original")
                self.pointer_path.rename(displaced)
                self.pointer_path.write_bytes(payload)
                self.pointer_path.chmod(0o600)
                racer_identity = (
                    self.pointer_path.stat().st_dev,
                    self.pointer_path.stat().st_ino,
                )
            return real_read(
                home,
                path,
                parent_fd,
                expected_identity=expected_identity,
            )

        with mock.patch.object(
            MODULE,
            "_read_managed_state_file_snapshot",
            side_effect=swap_pointer_before_bound_read,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "exact cleanup was incomplete"):
                MODULE._publish_pending_link_pointer(self.home, batch)

        self.assertIsNotNone(displaced)
        self.assertEqual(
            (self.pointer_path.stat().st_dev, self.pointer_path.stat().st_ino),
            racer_identity,
        )

    def test_pointer_clear_preserves_same_content_new_inode_racer(self) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        real_move = MODULE._move_managed_state_entry_to_quarantine
        racer_identity: tuple[int, int] | None = None

        def swap_before_move(
            home: Path,
            transaction: MODULE.ManagedStateFileTransaction,
            source_parent_fd: int,
            source_name: str,
            label: str,
            **kwargs: object,
        ) -> tuple[Path, bool]:
            nonlocal racer_identity
            if label == "pending-complete" and racer_identity is None:
                payload = self.pointer_path.read_bytes()
                displaced = self.pointer_path.with_name("pending-clear-original")
                self.pointer_path.rename(displaced)
                self.pointer_path.write_bytes(payload)
                self.pointer_path.chmod(0o600)
                racer_identity = (
                    self.pointer_path.stat().st_dev,
                    self.pointer_path.stat().st_ino,
                )
            return real_move(
                home,
                transaction,
                source_parent_fd,
                source_name,
                label,
                **kwargs,
            )

        with mock.patch.object(
            MODULE,
            "_move_managed_state_entry_to_quarantine",
            side_effect=swap_before_move,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "changed before quarantine"):
                MODULE._clear_pending_link_pointer(self.home, batch)

        self.assertIsNotNone(racer_identity)
        self.assertEqual(
            (self.pointer_path.stat().st_dev, self.pointer_path.stat().st_ino),
            racer_identity,
        )

    def test_pointer_and_state_evidence_precede_mutation_and_state_write(self) -> None:
        events: list[str] = []
        real_publish = MODULE._publish_pending_link_pointer
        real_apply = MODULE._apply_reconcile_actions
        real_write = MODULE._write_managed_state

        def publish(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> None:
            after_evidence = batch.batch_root / Path(
                *batch.state_after_evidence.parts
            )
            self.assertTrue(after_evidence.is_file())
            self.assertEqual(
                (after_evidence.stat().st_dev, after_evidence.stat().st_ino),
                batch.state_after.file_identity,
            )
            events.append("state-after")
            real_publish(home, batch)
            events.append("pointer")

        def apply(*args: object, **kwargs: object) -> MODULE.ReconcileTransaction | None:
            self.assertIn("pointer", events)
            events.append("mutation")
            return real_apply(*args, **kwargs)

        def write(
            home: Path,
            state: MODULE.ManagedState,
            transaction: MODULE.ManagedStateFileTransaction | None = None,
        ) -> MODULE.ManagedStateFileTransaction:
            self.assertIn("state-after", events)
            self.assertIsNotNone(transaction)
            assert transaction is not None
            pending = MODULE._load_pending_link_batch(home)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(transaction.batch_root, pending.batch_root)
            self.assertEqual(
                transaction.after_evidence_identity,
                pending.state_after.file_identity,
            )
            events.append("state-write")
            return real_write(home, state, transaction)

        with (
            mock.patch.object(MODULE, "_publish_pending_link_pointer", side_effect=publish),
            mock.patch.object(MODULE, "_apply_reconcile_actions", side_effect=apply),
            mock.patch.object(MODULE, "_write_managed_state", side_effect=write),
        ):
            install_quietly(self.release_b, self.home, SHA_B)

        self.assertLess(events.index("pointer"), events.index("mutation"))
        self.assertLess(events.index("state-after"), events.index("state-write"))

    def test_postcommit_pointer_clear_failure_never_runs_rollback(self) -> None:
        current = MODULE._current_link(self.home)
        managed_target = self.home / "skills" / "public-base"

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=MODULE.SyncError("injected pointer clear failure"),
            ),
            mock.patch.object(
                MODULE,
                "_restore_managed_state_file",
                wraps=MODULE._restore_managed_state_file,
            ) as restore_state,
            mock.patch.object(
                MODULE,
                "_rollback_reconcile_transaction",
                wraps=MODULE._rollback_reconcile_transaction,
            ) as rollback_links,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        restore_state.assert_not_called()
        rollback_links.assert_not_called()
        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)
        self.assertEqual(
            os.readlink(managed_target),
            "../personal-sync/current/personal_codex/skills/public-new",
        )
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_B},
        )

        install_quietly(self.release_b, self.home, SHA_B)

        self.assertFalse(os.path.lexists(self.pointer_path))
        self.assertEqual(os.readlink(current), f"releases/{SHA_B}")
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)

    def test_postcommit_current_inode_racer_retains_pointer_without_rollback(
        self,
    ) -> None:
        current = MODULE._current_link(self.home)
        real_publish_marker = MODULE._publish_pending_commit_marker
        injected = False
        racer_identity: tuple[int, int] | None = None

        def publish_marker_then_race_current(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> None:
            nonlocal injected, racer_identity
            real_publish_marker(home, batch)
            if not injected:
                injected = True
                link_target = os.readlink(current)
                current.unlink()
                current.symlink_to(link_target, target_is_directory=True)
                racer_identity = (current.lstat().st_dev, current.lstat().st_ino)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=publish_marker_then_race_current,
            ),
            mock.patch.object(
                MODULE,
                "_restore_managed_state_file",
                wraps=MODULE._restore_managed_state_file,
            ) as restore_state,
            mock.patch.object(
                MODULE,
                "_rollback_reconcile_transaction",
                wraps=MODULE._rollback_reconcile_transaction,
            ) as rollback_links,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        restore_state.assert_not_called()
        rollback_links.assert_not_called()
        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (current.lstat().st_dev, current.lstat().st_ino),
            racer_identity,
        )
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_B},
        )

    def test_partial_apply_rollback_failure_retains_pending_pointer(self) -> None:
        managed_target = self.home / "skills" / "public-base"
        real_move = MODULE._atomic_move_beneath_home
        injected = False
        foreign_identity: tuple[int, int] | None = None

        def fail_second_action_after_replacing_first(
            home: Path,
            source: Path,
            destination: Path,
            expected_snapshot: MODULE.ReconcileTargetSnapshot | None = None,
            expected_destination_parent_identity: tuple[int, int] | None = None,
        ) -> None:
            nonlocal foreign_identity, injected
            if not injected and source == managed_target:
                injected = True
                self.added_target.unlink()
                self.added_target.symlink_to(
                    "foreign-target",
                    target_is_directory=True,
                )
                foreign_identity = (
                    self.added_target.lstat().st_dev,
                    self.added_target.lstat().st_ino,
                )
                raise MODULE.SyncError("injected second action failure")
            real_move(
                home,
                source,
                destination,
                expected_snapshot,
                expected_destination_parent_identity,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=fail_second_action_after_replacing_first,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "installation failed.*rollback was incomplete",
            ):
                install_quietly(self.release_b, self.home, SHA_B)

        self.assertTrue(injected)
        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual(
            (self.added_target.lstat().st_dev, self.added_target.lstat().st_ino),
            foreign_identity,
        )
        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        added_record = next(
            record
            for record in batch.records
            if record.target == PurePosixPath("skills/added")
        )
        assert added_record.evidence is not None
        self.assertTrue(
            (batch.batch_root / Path(*added_record.evidence.parts)).is_symlink()
        )

    def test_install_replace_crash_restores_exact_current_and_managed_preimages(
        self,
    ) -> None:
        old_current = MODULE._read_symlink_snapshot_beneath(
            self.home,
            MODULE._current_link(self.home),
        )
        managed_target = self.home / "skills" / "public-base"
        old_managed = MODULE._read_symlink_snapshot_beneath(
            self.home,
            managed_target,
        )
        batch, actions, state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)

        recovered, _snapshot, did_recover = MODULE._recover_pending_link_transaction(
            self.home,
            state,
            state_snapshot,
            dry_run=False,
        )

        self.assertTrue(did_recover)
        self.assertEqual(recovered, state)
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                MODULE._current_link(self.home),
            ).link_identity,
            old_current.link_identity,
        )
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                managed_target,
            ).link_identity,
            old_managed.link_identity,
        )
        self.assertFalse(os.path.lexists(self.added_target))
        install_quietly(self.release_b, self.home, SHA_B)
        self.assertEqual(MODULE._current_sha(self.home), SHA_B)

    def test_zero_create_replace_transaction_is_wal_protected(self) -> None:
        binding = MODULE._stage_release_tree_for_install(
            self.release_b,
            self.home,
            SHA_B,
            self.manifest_b,
        )
        MODULE._close_install_release_bindings([binding])
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
        current_manifest = MODULE._current_manifest_data(self.home, MODULE.PUBLIC_OWNER)
        desired_entries = [
            entry
            for entry in self.manifest_b.entries
            if entry.target == PurePosixPath("skills/public-base")
        ]
        actions = MODULE._plan_reconciliation(
            self.home,
            desired_entries,
            current_manifest.entries,
            [],
            state,
            allow_cross_owner=False,
        )
        self.assertEqual([action.action for action in actions], ["replace"])
        current_action = MODULE._plan_current_switch_action(self.home, SHA_B)
        self.assertIsNotNone(current_action)
        assert current_action is not None
        next_state = MODULE._planned_committed_state(
            self.home,
            desired_entries,
            {MODULE.PUBLIC_OWNER: SHA_B},
            {PurePosixPath("skills/public-base")},
        )
        batch = MODULE._stage_pending_link_batch(
            self.home,
            [("current", [current_action]), ("managed", actions)],
            desired_entries,
            {MODULE.PUBLIC_OWNER: SHA_B},
            state_snapshot,
            state,
            next_state,
        )
        self.assertNotIn("create", {record.action for record in batch.records})
        MODULE._publish_pending_link_pointer(self.home, batch)
        MODULE._apply_reconcile_actions(
            self.home,
            [current_action],
            dry_run=False,
            pending_batch=batch,
            pending_scope="current",
            batch_root=batch.batch_root,
        )
        MODULE._apply_reconcile_actions(
            self.home,
            actions,
            dry_run=False,
            pending_batch=batch,
            pending_scope="managed",
            batch_root=batch.batch_root,
        )

        recovered, _snapshot, _did_recover = MODULE._recover_pending_link_transaction(
            self.home,
            state,
            state_snapshot,
            dry_run=False,
        )

        self.assertEqual(recovered, state)
        self.assertEqual(MODULE._current_sha(self.home), SHA_A)

    def test_remove_only_crash_restores_exact_old_link(self) -> None:
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
        target = self.home / "skills" / "public-base"
        old_snapshot = MODULE._read_symlink_snapshot_beneath(self.home, target)
        action = MODULE.ReconcileAction(
            "remove",
            target,
            "",
            "skill",
            expected_link_target=old_snapshot.link_target,
            planned_snapshot=MODULE._capture_reconcile_target_snapshot(
                self.home,
                target,
            ),
        )
        next_state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: SHA_A},
            links={},
        )
        batch = MODULE._stage_pending_link_batch(
            self.home,
            [("managed", [action])],
            [],
            {MODULE.PUBLIC_OWNER: SHA_A},
            state_snapshot,
            state,
            next_state,
        )
        MODULE._publish_pending_link_pointer(self.home, batch)
        MODULE._apply_reconcile_actions(
            self.home,
            [action],
            dry_run=False,
            pending_batch=batch,
            pending_scope="managed",
            batch_root=batch.batch_root,
        )
        self.assertFalse(os.path.lexists(target))

        MODULE._recover_pending_link_transaction(
            self.home,
            state,
            state_snapshot,
            dry_run=False,
        )

        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                target,
            ).link_identity,
            old_snapshot.link_identity,
        )

    def test_same_payload_state_inode_selects_rollback_or_finalize(self) -> None:
        target = self.home / "skills" / "unclaimed-legacy"
        target.symlink_to("legacy-source", target_is_directory=True)

        def stage_removal() -> tuple[
            MODULE.PendingLinkBatch,
            MODULE.ManagedState,
            MODULE.ManagedStateFileSnapshot,
            MODULE.SymlinkSnapshot,
        ]:
            state, state_snapshot = MODULE._load_managed_state_with_snapshot(
                self.home
            )
            old_link = MODULE._read_symlink_snapshot_beneath(self.home, target)
            action = MODULE.ReconcileAction(
                "quarantine-remove",
                target,
                "",
                "skill",
                expected_link_target=old_link.link_target,
                planned_snapshot=MODULE._capture_reconcile_target_snapshot(
                    self.home,
                    target,
                ),
            )
            batch = MODULE._stage_pending_link_batch(
                self.home,
                [("managed", [action])],
                [],
                state.owners,
                state_snapshot,
                state,
                state,
            )
            self.assertEqual(
                batch.state_before.payload,
                batch.state_after.payload,
            )
            self.assertNotEqual(
                batch.state_before.file_identity,
                batch.state_after.file_identity,
            )
            MODULE._publish_pending_link_pointer(self.home, batch)
            MODULE._apply_reconcile_actions(
                self.home,
                [action],
                dry_run=False,
                pending_batch=batch,
                pending_scope="managed",
                batch_root=batch.batch_root,
            )
            self.assertFalse(os.path.lexists(target))
            return batch, state, state_snapshot, old_link

        _batch, state, state_snapshot, old_link = stage_removal()
        recovered, _snapshot, did_recover = MODULE._recover_pending_link_transaction(
            self.home,
            state,
            state_snapshot,
            dry_run=False,
        )

        self.assertTrue(did_recover)
        self.assertEqual(recovered, state)
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                target,
            ).link_identity,
            old_link.link_identity,
        )
        self.assertFalse(os.path.lexists(self.pointer_path))

        batch, _state, state_snapshot, _old_link = stage_removal()
        self._publish_committed_state(batch, state_snapshot)
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        self.assertEqual(committed_state, batch.state_after_value)
        self.assertEqual(
            committed_snapshot.file_identity,
            batch.state_after.file_identity,
        )

        recovered, recovered_snapshot, did_recover = (
            MODULE._recover_pending_link_transaction(
                self.home,
                committed_state,
                committed_snapshot,
                dry_run=False,
            )
        )

        self.assertTrue(did_recover)
        self.assertEqual(recovered, committed_state)
        self.assertEqual(
            recovered_snapshot.file_identity,
            batch.state_after.file_identity,
        )
        self.assertFalse(os.path.lexists(target))
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_uncommitted_replace_new_inode_racer_blocks_and_is_preserved(self) -> None:
        batch, actions, state, state_snapshot = self._stage_crash_batch()
        self._publish_pending_target(batch, actions)
        target = self.home / "skills" / "public-base"
        link_target = os.readlink(target)
        target.unlink()
        target.symlink_to(link_target, target_is_directory=True)
        racer_identity = (target.lstat().st_dev, target.lstat().st_ino)

        with self.assertRaisesRegex(MODULE.SyncError, "foreign target"):
            MODULE._recover_pending_link_transaction(
                self.home,
                state,
                state_snapshot,
                dry_run=False,
            )

        self.assertTrue(self.pointer_path.is_file())
        self.assertEqual((target.lstat().st_dev, target.lstat().st_ino), racer_identity)

    def test_uninstall_crash_rolls_back_exactly_and_committed_uninstall_finalizes(
        self,
    ) -> None:
        private_release = self.root / "private-release"
        private_manifest = write_skill_release(
            private_release,
            source_name="private-base",
            target_name="private-base",
            owner="private",
        )
        install_quietly(private_release, self.home, SHA_B)
        private_target = self.home / "skills" / "private-base"
        private_current = MODULE._current_link(self.home, "private")

        def stage_uninstall() -> tuple[
            MODULE.PendingLinkBatch,
            list[MODULE.ReconcileAction],
            MODULE.ReconcileAction,
            MODULE.ManagedState,
            MODULE.ManagedStateFileSnapshot,
        ]:
            state, state_snapshot = MODULE._load_managed_state_with_snapshot(self.home)
            public_manifest = MODULE._current_manifest_data(
                self.home,
                MODULE.PUBLIC_OWNER,
            )
            actions = MODULE._plan_reconciliation(
                self.home,
                public_manifest.entries,
                [*public_manifest.entries, *private_manifest.entries],
                [],
                state,
                allow_cross_owner=True,
            )
            current_snapshot = MODULE._capture_reconcile_target_snapshot(
                self.home,
                private_current,
            )
            assert current_snapshot.link_target is not None
            current_action = MODULE.ReconcileAction(
                "remove",
                private_current,
                "",
                "directory",
                expected_link_target=current_snapshot.link_target,
                planned_snapshot=current_snapshot,
            )
            next_state = MODULE._planned_committed_state(
                self.home,
                public_manifest.entries,
                {MODULE.PUBLIC_OWNER: SHA_A},
                MODULE._managed_targets_after_reconciliation(
                    self.home,
                    state,
                    actions,
                ),
            )
            batch = MODULE._stage_pending_link_batch(
                self.home,
                [("managed", actions), ("current", [current_action])],
                public_manifest.entries,
                {MODULE.PUBLIC_OWNER: SHA_A},
                state_snapshot,
                state,
                next_state,
            )
            MODULE._publish_pending_link_pointer(self.home, batch)
            return batch, actions, current_action, state, state_snapshot

        old_link = MODULE._read_symlink_snapshot_beneath(self.home, private_target)
        old_current = MODULE._read_symlink_snapshot_beneath(self.home, private_current)
        batch, actions, current_action, state, state_snapshot = stage_uninstall()
        MODULE._apply_reconcile_actions(
            self.home,
            actions,
            dry_run=False,
            pending_batch=batch,
            pending_scope="managed",
            batch_root=batch.batch_root,
        )
        MODULE._apply_reconcile_actions(
            self.home,
            [current_action],
            dry_run=False,
            pending_batch=batch,
            pending_scope="current",
            batch_root=batch.batch_root,
        )

        MODULE._recover_pending_link_transaction(
            self.home,
            state,
            state_snapshot,
            dry_run=False,
        )

        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_target,
            ).link_identity,
            old_link.link_identity,
        )
        self.assertEqual(
            MODULE._read_symlink_snapshot_beneath(
                self.home,
                private_current,
            ).link_identity,
            old_current.link_identity,
        )

        batch, actions, current_action, _state, _snapshot = stage_uninstall()
        MODULE._apply_reconcile_actions(
            self.home,
            actions,
            dry_run=False,
            pending_batch=batch,
            pending_scope="managed",
            batch_root=batch.batch_root,
        )
        MODULE._apply_reconcile_actions(
            self.home,
            [current_action],
            dry_run=False,
            pending_batch=batch,
            pending_scope="current",
            batch_root=batch.batch_root,
        )
        state_transaction = MODULE._prepare_pending_managed_state_transaction(
            self.home,
            batch,
            batch.state_after_value,
        )
        MODULE._write_managed_state(
            self.home,
            batch.state_after_value,
            state_transaction,
        )
        MODULE._publish_pending_commit_marker(self.home, batch)
        committed_state, committed_snapshot = MODULE._load_managed_state_with_snapshot(
            self.home
        )
        MODULE._recover_pending_link_transaction(
            self.home,
            committed_state,
            committed_snapshot,
            dry_run=False,
        )
        self.assertFalse(os.path.lexists(private_target))
        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_public_preflight_reaches_locked_recovery_with_missing_current(self) -> None:
        batch, _actions, _state, _state_snapshot = self._stage_crash_batch()
        current_record = next(
            record for record in batch.records if record.scope == "current"
        )
        assert current_record.backup is not None
        current = MODULE._current_link(self.home)
        backup = batch.batch_root / Path(*current_record.backup.parts)
        backup_parent_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            backup.parent,
        )
        try:
            backup_parent_identity = MODULE._directory_identity(backup_parent_fd)
        finally:
            MODULE._close_fd_quietly(backup_parent_fd)
        MODULE._atomic_move_beneath_home(
            self.home,
            current,
            backup,
            current_record.planned_snapshot,
            backup_parent_identity,
        )
        self.assertFalse(os.path.lexists(current))

        install_quietly(self.release_b, self.home, SHA_B)

        self.assertEqual(MODULE._current_sha(self.home), SHA_B)
        self.assertFalse(os.path.lexists(self.pointer_path))

    def test_empty_record_empty_state_transaction_parses_and_recovers(self) -> None:
        empty_home = self.root / "empty-home"
        empty_home.mkdir()
        MODULE._ensure_safe_internal_parent(
            empty_home,
            MODULE._state_path(empty_home),
            create=True,
        )
        state, state_snapshot = MODULE._load_managed_state_with_snapshot(empty_home)
        batch = MODULE._stage_pending_link_batch(
            empty_home,
            [],
            [],
            {},
            state_snapshot,
            state,
            state,
        )
        MODULE._publish_pending_link_pointer(empty_home, batch)
        parsed = MODULE._load_pending_link_batch(empty_home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.records, ())

        recovered, _snapshot, did_recover = MODULE._recover_pending_link_transaction(
            empty_home,
            state,
            state_snapshot,
            dry_run=False,
        )

        self.assertTrue(did_recover)
        self.assertEqual(recovered, state)
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(empty_home))
        )

    def test_oversized_json_integer_is_normalized_to_sync_error(self) -> None:
        payload = b'{"value":' + (b"9" * 5000) + b"}"
        with self.assertRaisesRegex(MODULE.SyncError, "Invalid JSON"):
            MODULE._decode_managed_state_json(payload, self.state_path)


class OverlayUninstallReplacementSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.public_release = self.root / "public-release"
        write_skill_release(
            self.public_release,
            source_name="public-base",
            target_name="public-base",
        )
        install_quietly(self.public_release, self.home, SHA_A)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _set_removed_links(
        self,
        release_root: Path,
        removed_links: list[dict[str, object]],
    ) -> MODULE.ManifestData:
        manifest_path = release_root / MODULE.MANIFEST_RELATIVE_PATH
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["removed_links"] = removed_links
        manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return MODULE.load_manifest_data(release_root)

    def test_uninstall_drops_outgoing_internal_replacement_obligation(self) -> None:
        private_release = self.root / "private-release"
        write_skill_release(
            private_release,
            source_name="private-new",
            target_name="private-new",
            owner="private",
        )
        self._set_removed_links(
            private_release,
            [
                {
                    "id": "internal-migration",
                    "source": "personal_codex/skills/private-old",
                    "target": "skills/private-old",
                    "kind": "skill",
                    "replacement_target": "skills/private-new",
                    "legacy": True,
                }
            ],
        )
        install_quietly(private_release, self.home, SHA_B)

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(self.home / "skills" / "private-new"))
        self.assertTrue((self.home / "skills" / "public-base").is_symlink())
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners, {MODULE.PUBLIC_OWNER: SHA_A})

    def test_uninstall_rejects_removing_unique_replacement_retirement(self) -> None:
        public_release = self.root / "public-with-active-removal"
        private_release = self.root / "private-retirement"
        write_skill_release(
            public_release,
            source_name="public-next",
            target_name="public-base",
        )
        public_manifest = self._set_removed_links(
            public_release,
            [
                {
                    "id": "requires-retirement",
                    "source": "personal_codex/skills/legacy",
                    "target": "skills/legacy",
                    "kind": "skill",
                    "replacement_target": "skills/retired-target",
                    "legacy": True,
                }
            ],
        )
        write_skill_release(
            private_release,
            source_name="private-keeper",
            target_name="private-keeper",
            owner="private",
        )
        private_manifest = self._set_removed_links(
            private_release,
            [
                {
                    "id": "retire-public-replacement",
                    "source": "personal_codex/skills/retired-target",
                    "target": "skills/retired-target",
                    "kind": "skill",
                    "retires_replacements": ["public:requires-retirement"],
                    "legacy": True,
                }
            ],
        )
        sha_c = "c" * 40
        with (
            MODULE.installation_lock(self.home),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE._install_release_set_unlocked(
                self.home,
                [
                    (public_release, sha_c, public_manifest),
                    (private_release, SHA_B, private_manifest),
                ],
                dry_run=False,
                allow_cross_owner=True,
            )

        private_current = (
            self.home / "personal-sync" / "overlays" / "private" / "current"
        )
        state_path = self.home / "personal-sync" / "state" / "managed-links.json"
        before = (
            os.readlink(private_current),
            os.readlink(self.home / "skills" / "private-keeper"),
            state_path.read_bytes(),
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "replacement target skills/retired-target is unavailable",
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        after = (
            os.readlink(private_current),
            os.readlink(self.home / "skills" / "private-keeper"),
            state_path.read_bytes(),
        )
        self.assertEqual(after, before)


class SchedulerInternalPathSafetyTests(unittest.TestCase):
    def test_rejects_symlinked_logs_before_writing_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / ".codex"
            outside = Path(temp_dir) / "outside"
            outside.mkdir(parents=True)
            sync_root = home / "personal-sync"
            sync_root.mkdir(parents=True)
            (sync_root / "logs").symlink_to(outside, target_is_directory=True)
            paths = MODULE.SchedulerPaths(
                platform="macos",
                launchd_plist=Path(temp_dir) / "LaunchAgents" / "sync.plist",
            )

            with (
                mock.patch.object(
                    MODULE,
                    "_scheduler_paths",
                    return_value=paths,
                ),
                mock.patch.object(MODULE, "_validate_scheduler_runner"),
                mock.patch.object(MODULE, "_write_plist") as write_plist,
            ):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "refusing unsafe directory path",
                ):
                    MODULE.install_scheduler(
                        home,
                        "Joey-Tools/codex-toolbox",
                        60,
                        "macos",
                        "/tmp/runner",
                        dry_run=False,
                        enable=False,
                    )

            write_plist.assert_not_called()


class ManifestPathEncodingSafetyTests(unittest.TestCase):
    def test_runtime_rejects_json_nul_and_lone_surrogate_paths(self) -> None:
        payloads = {
            "source-nul": rb'{"version":1,"links":[{"source":"personal_codex/skills\u0000/example","target":"skills/example","kind":"skill"}]}',
            "source-surrogate": rb'{"version":1,"links":[{"source":"personal_codex/skills/\ud800","target":"skills/example","kind":"skill"}]}',
            "target-nul": rb'{"version":1,"links":[{"source":"personal_codex/skills/example","target":"skills\u0000/example","kind":"skill"}]}',
            "target-surrogate": rb'{"version":1,"links":[{"source":"personal_codex/skills/example","target":"skills/\ud800","kind":"skill"}]}',
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                data = json.loads(payload.decode("utf-8"))
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "embedded NUL|valid UTF-8",
                ):
                    MODULE._parse_manifest_data(data, lambda _path: "directory")

    def test_runtime_rejects_escaped_surrogate_outside_paths(self) -> None:
        data = json.loads(
            rb'{"version":1,"base_release":{"repo":"owner/\ud800"},"links":[{"source":"personal_codex/skills/example","target":"skills/example","kind":"skill"}]}'.decode(
                "utf-8"
            )
        )

        with self.assertRaisesRegex(MODULE.SyncError, "not valid UTF-8"):
            MODULE._parse_manifest_data(data, lambda _path: "directory")

    def test_runtime_translates_path_kind_os_errors(self) -> None:
        data = json.loads(
            rb'{"version":1,"links":[{"source":"personal_codex/skills/example","target":"skills/example","kind":"skill"}]}'.decode(
                "utf-8"
            )
        )

        for error in (ValueError("embedded null byte"), OSError("unreadable path")):
            with self.subTest(error=type(error).__name__):
                def invalid_os_path(_path: PurePosixPath) -> str | None:
                    raise error

                with self.assertRaisesRegex(MODULE.SyncError, "valid filesystem path"):
                    MODULE._parse_manifest_data(data, invalid_os_path)


if __name__ == "__main__":
    unittest.main()
