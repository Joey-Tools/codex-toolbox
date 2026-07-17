from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_personal_codex_package.py"
SPEC = importlib.util.spec_from_file_location("package_builder_safety", SCRIPT_PATH)
BUILDER = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)


class PackageBuilderSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="package-builder-safety.")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rejects_non_integer_manifest_versions(self) -> None:
        for version in (True, 1.0):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(BUILDER.PackageError, "version must be 1"),
            ):
                BUILDER._manifest_sources({"version": version, "links": []})

    def git(self, repo: Path, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, repo: Path, message: str) -> str:
        self.git(
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--no-gpg-sign",
            "-qm",
            message,
        )
        return self.git(repo, "rev-parse", "HEAD")

    def write_manifest(self, repo: Path, source: str, manifest: str = "manifest.json") -> Path:
        manifest_path = repo / manifest
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "links": [
                        {
                            "source": source,
                            "target": "skills/example",
                            "kind": "skill",
                        }
                    ],
                    "reference_only": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return Path(manifest)

    def init_tracked_repo(
        self,
        name: str = "repo",
    ) -> tuple[Path, Path, Path, str]:
        repo = self.root / name
        source = repo / "payload" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("---\nname: example\n---\n", encoding="utf-8")
        manifest = self.write_manifest(repo, "payload")
        self.git(repo, "init", "-q")
        self.git(repo, "add", manifest.as_posix(), "payload/SKILL.md")
        head = self.commit(repo, "Add package inputs")
        return repo, manifest, source, head

    def strict_build(self, repo: Path, manifest: Path, head: str) -> tuple[Path, Path]:
        return BUILDER.build_package(
            repo,
            manifest,
            repo / "dist",
            head,
            require_clean_sources=True,
        )

    def archive_member(
        self,
        archive: Path,
        head: str,
        relative_path: str,
    ) -> tuple[bytes, int]:
        member_name = f"personal-codex-{head}/{relative_path}"
        with tarfile.open(archive, "r:gz") as package:
            member = package.getmember(member_name)
            extracted = package.extractfile(member)
            self.assertIsNotNone(extracted)
            assert extracted is not None
            return extracted.read(), member.mode

    def test_rejects_manifest_source_with_symlink_ancestor(self) -> None:
        repo = self.root / "repo"
        outside = self.root / "outside"
        (outside / "example").mkdir(parents=True)
        (outside / "example" / "SKILL.md").write_text("secret\n", encoding="utf-8")
        repo.mkdir()
        (repo / "linked").symlink_to(outside, target_is_directory=True)
        manifest = self.write_manifest(repo, "linked/example")

        with self.assertRaisesRegex(BUILDER.PackageError, "symlink path component"):
            BUILDER.stage_release(repo, manifest, self.root / "staging")

    def test_rejects_manifest_with_symlink_ancestor(self) -> None:
        repo = self.root / "repo"
        outside = self.root / "outside"
        (repo / "payload").mkdir(parents=True)
        (repo / "payload" / "SKILL.md").write_text("skill\n", encoding="utf-8")
        outside.mkdir()
        self.write_manifest(outside, "payload", "manifest.json")
        repo.mkdir(exist_ok=True)
        (repo / "linked").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(BUILDER.PackageError, "symlink path component"):
            BUILDER.stage_release(repo, Path("linked/manifest.json"), self.root / "staging")

    def test_rejects_nested_git_marker_in_source_ancestor(self) -> None:
        repo = self.root / "repo"
        nested = repo / "nested"
        nested.mkdir(parents=True)
        (nested / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        (nested / "SKILL.md").write_text("skill\n", encoding="utf-8")
        manifest = self.write_manifest(repo, "nested/SKILL.md")

        with self.assertRaisesRegex(BUILDER.PackageError, "nested Git repository"):
            BUILDER.stage_release(repo, manifest, self.root / "staging")

    def test_strict_mode_binds_sha_index_and_worktree_to_head(self) -> None:
        repo, manifest, source, head = self.init_tracked_repo()
        archive, checksum = self.strict_build(repo, manifest, head)
        self.assertTrue(archive.is_file())
        self.assertTrue(checksum.is_file())

        with self.assertRaisesRegex(BUILDER.PackageError, "40 lowercase hexadecimal"):
            BUILDER.build_package(
                repo,
                manifest,
                repo / "dist-short",
                head[:12],
                require_clean_sources=True,
            )
        with self.assertRaisesRegex(BUILDER.PackageError, "does not match HEAD"):
            BUILDER.build_package(
                repo,
                manifest,
                repo / "dist-wrong",
                "f" * 40,
                require_clean_sources=True,
            )

        source.write_text("worktree drift\n", encoding="utf-8")
        with self.assertRaisesRegex(BUILDER.PackageError, "worktree differs"):
            self.strict_build(repo, manifest, head)
        self.git(repo, "restore", "payload/SKILL.md")

        source.write_text("index drift\n", encoding="utf-8")
        self.git(repo, "add", "payload/SKILL.md")
        with self.assertRaisesRegex(BUILDER.PackageError, "indexed manifest sources differ"):
            self.strict_build(repo, manifest, head)

    def test_strict_mode_ignores_unselected_siblings_under_inventory_root(self) -> None:
        repo = self.root / "repo"
        selected = repo / "payload" / "selected"
        selected.mkdir(parents=True)
        (selected / "SKILL.md").write_text("skill\n", encoding="utf-8")
        unrelated = repo / "payload" / "unrelated.txt"
        unrelated.write_text("committed\n", encoding="utf-8")
        manifest = self.write_manifest(repo, "payload/selected")
        self.git(repo, "init", "-q")
        self.git(repo, "add", ".")
        head = self.commit(repo, "Add scoped package inputs")

        unrelated.write_text("worktree drift\n", encoding="utf-8")
        (repo / "payload" / "local.txt").write_text(
            "untracked sibling\n",
            encoding="utf-8",
        )

        archive, checksum = self.strict_build(repo, manifest, head)

        self.assertTrue(archive.is_file())
        self.assertTrue(checksum.is_file())

    def test_strict_build_ignores_commit_and_blob_replacement_refs(self) -> None:
        repo, manifest, source, original_head = self.init_tracked_repo()
        original_payload = source.read_bytes()
        original_blob = self.git(repo, "rev-parse", "HEAD:payload/SKILL.md")
        source.write_text("replacement payload\n", encoding="utf-8")
        self.git(repo, "add", "payload/SKILL.md")
        replacement_head = self.commit(repo, "Add replacement payload")
        replacement_blob = self.git(repo, "rev-parse", "HEAD:payload/SKILL.md")
        self.git(repo, "switch", "--detach", original_head)
        self.git(repo, "replace", original_head, replacement_head)
        self.git(repo, "replace", original_blob, replacement_blob)

        archive, _checksum = self.strict_build(repo, manifest, original_head)

        packaged_payload, _mode = self.archive_member(
            archive,
            original_head,
            "payload/SKILL.md",
        )
        self.assertEqual(packaged_payload, original_payload)

    def test_archive_contains_deterministic_package_root(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo()
        archive, _checksum = self.strict_build(repo, manifest, head)

        with tarfile.open(archive, "r:gz") as package:
            package_root = package.getmember(f"personal-codex-{head}")

        self.assertTrue(package_root.isdir())
        self.assertEqual(package_root.mode, 0o755)

    def test_strict_staging_uses_commit_blob_for_assume_unchanged_file(self) -> None:
        repo, manifest, source, head = self.init_tracked_repo()
        committed_payload = source.read_bytes()
        source.write_text("assume-unchanged drift\n", encoding="utf-8")
        self.git(repo, "update-index", "--assume-unchanged", "--", "payload/SKILL.md")

        archive, _checksum = self.strict_build(repo, manifest, head)

        archived_payload, archived_mode = self.archive_member(
            archive,
            head,
            "payload/SKILL.md",
        )
        self.assertEqual(archived_payload, committed_payload)
        self.assertEqual(archived_mode, 0o644)

    def test_strict_staging_uses_validated_blobs_after_index_and_worktree_mutation(
        self,
    ) -> None:
        repo, manifest, source, head = self.init_tracked_repo()
        committed_payload = source.read_bytes()
        original_stage = BUILDER.stage_strict_release

        def mutate_then_stage(
            repo_root: Path,
            snapshot: object,
            staging_root: Path,
        ) -> None:
            source.write_text("post-validation drift\n", encoding="utf-8")
            (repo / manifest).write_text("{ invalid after validation\n", encoding="utf-8")
            (repo / manifest).chmod(0o755)
            self.git(repo, "add", manifest.as_posix(), "payload/SKILL.md")
            self.git(repo, "update-index", "--chmod=+x", "--", manifest.as_posix())
            original_stage(repo_root, snapshot, staging_root)

        with mock.patch.object(
            BUILDER,
            "stage_strict_release",
            side_effect=mutate_then_stage,
        ):
            archive, _checksum = self.strict_build(repo, manifest, head)

        archived_payload, _mode = self.archive_member(
            archive,
            head,
            "payload/SKILL.md",
        )
        archived_manifest, archived_manifest_mode = self.archive_member(
            archive,
            head,
            "personal_codex/sync-manifest.json",
        )
        self.assertEqual(archived_payload, committed_payload)
        self.assertEqual(json.loads(archived_manifest)["links"][0]["source"], "payload")
        self.assertEqual(archived_manifest_mode, 0o644)

    def test_strict_staging_preserves_committed_executable_manifest_mode(self) -> None:
        repo, manifest, _source, _head = self.init_tracked_repo()
        (repo / manifest).chmod(0o755)
        self.git(repo, "update-index", "--chmod=+x", "--", manifest.as_posix())
        head = self.commit(repo, "Make package manifest executable")

        archive, _checksum = self.strict_build(repo, manifest, head)

        _archived_manifest, archived_mode = self.archive_member(
            archive,
            head,
            "personal_codex/sync-manifest.json",
        )
        self.assertEqual(archived_mode, 0o755)

    def test_strict_snapshot_binds_stage_zero_mode_to_head(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo()
        self.git(repo, "update-index", "--chmod=+x", "payload/SKILL.md")

        with self.assertRaisesRegex(BUILDER.PackageError, "indexed manifest sources differ"):
            self.strict_build(repo, manifest, head)

    def test_strict_snapshot_rejects_portable_path_conflicts(self) -> None:
        cases = {
            "case-file": ("payload/skill.md",),
            "unicode-file": (
                "payload/caf\N{LATIN SMALL LETTER E WITH ACUTE}.md",
                "payload/cafe\N{COMBINING ACUTE ACCENT}.md",
            ),
            "case-directory": (
                "payload/Foo/one.md",
                "payload/foo/two.md",
            ),
            "unicode-directory": (
                "payload/caf\N{LATIN SMALL LETTER E WITH ACUTE}/one.md",
                "payload/cafe\N{COMBINING ACUTE ACCENT}/two.md",
            ),
            "file-directory": (
                "payload/Thing",
                "payload/thing/child.md",
            ),
        }
        for name, conflicting_paths in cases.items():
            with self.subTest(case=name):
                repo, manifest, _source, head = self.init_tracked_repo(
                    f"repo-{name}"
                )
                self.git(repo, "config", "core.precomposeunicode", "false")
                blob = self.git(repo, "rev-parse", f"{head}:payload/SKILL.md")
                for path in conflicting_paths:
                    self.git(
                        repo,
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"100644,{blob},{path}",
                    )
                head = self.commit(repo, "Add conflicting package paths")

                with self.assertRaisesRegex(
                    BUILDER.PackageError,
                    "portable path conflict",
                ):
                    self.strict_build(repo, manifest, head)

    def test_strict_mode_requires_unique_stage_zero_regular_entry(self) -> None:
        repo, manifest, _source, _head = self.init_tracked_repo()
        blob = self.git(repo, "rev-parse", "HEAD:payload/SKILL.md")
        self.git(repo, "update-index", "--force-remove", "payload/SKILL.md")
        self.git(
            repo,
            "update-index",
            "--index-info",
            input_text=(
                f"100644 {blob} 1\tpayload/SKILL.md\n"
            ),
        )

        with self.assertRaisesRegex(BUILDER.PackageError, "stage-0 regular index entry"):
            BUILDER.ensure_manifest_sources_are_strictly_tracked(repo, manifest)

    def test_strict_mode_rejects_gitlink_equal_ancestor_and_descendant(self) -> None:
        for relation, source in {
            "equal": "modules/module",
            "ancestor": "modules/module/SKILL.md",
            "descendant": "modules",
        }.items():
            with self.subTest(relation=relation):
                repo = self.root / relation
                repo.mkdir()
                self.git(repo, "init", "-q")
                (repo / "README.md").write_text("base\n", encoding="utf-8")
                self.git(repo, "add", "README.md")
                target_commit = self.commit(repo, "Add base")
                manifest = self.write_manifest(repo, source)
                module = repo / "modules" / "module"
                module.mkdir(parents=True)
                (module / "SKILL.md").write_text("skill\n", encoding="utf-8")
                self.git(repo, "add", manifest.as_posix())
                self.git(
                    repo,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{target_commit},modules/module",
                )

                with self.assertRaisesRegex(BUILDER.PackageError, "gitlink"):
                    BUILDER.ensure_manifest_sources_are_strictly_tracked(repo, manifest)

    def test_strict_mode_distinguishes_untracked_ignored_and_force_added(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo()
        local = repo / "payload" / "local.txt"
        local.write_text("local\n", encoding="utf-8")
        with self.assertRaisesRegex(BUILDER.PackageError, "untracked files"):
            self.strict_build(repo, manifest, head)

        (repo / ".gitignore").write_text("payload/local.txt\n*.pyc\n", encoding="utf-8")
        self.git(repo, "add", ".gitignore")
        head = self.commit(repo, "Ignore local package files")
        with self.assertRaisesRegex(BUILDER.PackageError, "untracked files"):
            self.strict_build(repo, manifest, head)

        self.git(repo, "add", "-f", "payload/local.txt")
        head = self.commit(repo, "Track ignored package file")
        generated = repo / "payload" / "__pycache__" / "cache.pyc"
        generated.parent.mkdir()
        generated.write_bytes(b"generated")
        archive, checksum = self.strict_build(repo, manifest, head)
        self.assertTrue(archive.is_file())
        self.assertTrue(checksum.is_file())

    def test_manifest_owner_and_override_match_runtime_semantics(self) -> None:
        def manifest(*, owner: object = "missing") -> dict[str, object]:
            payload: dict[str, object] = {
                "version": 1,
                "links": [
                    {
                        "source": "payload",
                        "target": "skills/example",
                        "kind": "skill",
                    }
                ],
            }
            if owner != "missing":
                payload["owner"] = owner
            return payload

        public_default = manifest()
        self.assertEqual(
            BUILDER._manifest_sources(public_default),
            [Path("payload")],
        )

        private_default_link = manifest(owner="private")
        private_default_link["links"][0]["override"] = True  # type: ignore[index]
        self.assertEqual(
            BUILDER._manifest_sources(private_default_link),
            [Path("payload")],
        )

        owner_at_limit = "o" * BUILDER.MAX_OWNER_COMPONENT_BYTES
        self.assertEqual(
            BUILDER._manifest_sources(manifest(owner=owner_at_limit)),
            [Path("payload")],
        )

        invalid_cases: list[tuple[str, dict[str, object], str]] = []
        explicit_null_owner = manifest(owner=None)
        invalid_cases.append(("manifest-owner-null", explicit_null_owner, "owner id"))

        explicit_null_link_owner = manifest(owner="private")
        explicit_null_link_owner["links"][0]["owner"] = None  # type: ignore[index]
        invalid_cases.append(("link-owner-null", explicit_null_link_owner, "owner id"))

        mismatched_link_owner = manifest(owner="private")
        mismatched_link_owner["links"][0]["owner"] = "other"  # type: ignore[index]
        invalid_cases.append(("link-owner-mismatch", mismatched_link_owner, "does not match"))

        invalid_owner = manifest(owner="private/invalid")
        invalid_cases.append(("invalid-owner", invalid_owner, "owner id"))

        overlong_owner = manifest(owner=owner_at_limit + "o")
        invalid_cases.append(
            ("overlong-owner", overlong_owner, "must not exceed 255")
        )

        null_override = manifest(owner="private")
        null_override["links"][0]["override"] = None  # type: ignore[index]
        invalid_cases.append(("override-null", null_override, "override must be boolean"))

        public_override = manifest()
        public_override["links"][0]["override"] = True  # type: ignore[index]
        invalid_cases.append(("public-override", public_override, "must not declare"))

        for name, payload, message in invalid_cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(BUILDER.PackageError, message):
                    BUILDER._manifest_sources(payload)

    def test_manifest_enforces_active_managed_link_target_byte_limit(self) -> None:
        owner = "o" * BUILDER.MAX_OWNER_COMPONENT_BYTES
        target = "/".join(["t"] * BUILDER.MAX_MANIFEST_TARGET_PATH_DEPTH)
        removed_target = "/".join(
            ["r"] * BUILDER.MAX_MANIFEST_TARGET_PATH_DEPTH
        )
        source_at_limit = "/".join(["s" * 181, "s" * 181, "s" * 183])
        source_over_limit = "/".join(["s" * 181, "s" * 181, "s" * 184])
        unicode_source_at_limit = "/".join(
            ["é" * 127, "é" * 127, "é" * 18 + "s"]
        )
        unicode_source_over_limit = "/".join(
            ["é" * 127, "é" * 127, "é" * 18 + "ss"]
        )
        payload: dict[str, object] = {
            "version": 1,
            "owner": owner,
            "links": [
                {
                    "source": source_at_limit,
                    "target": target,
                    "kind": "directory",
                }
            ],
            "removed_links": [
                {
                    "id": "legacy-long-target",
                    "source": source_over_limit,
                    "target": removed_target,
                    "kind": "directory",
                }
            ],
        }

        self.assertEqual(
            len(
                BUILDER._relative_managed_link_target(
                    PurePosixPath(source_at_limit),
                    PurePosixPath(target),
                    owner,
                ).encode("utf-8")
            ),
            BUILDER.MAX_MANAGED_LINK_TARGET_BYTES,
        )
        self.assertEqual(
            BUILDER._manifest_sources(payload),
            [Path(source_at_limit)],
        )

        payload["links"][0]["source"] = source_over_limit  # type: ignore[index]
        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "managed symlink target exceeds 1023 UTF-8 bytes",
        ):
            BUILDER._manifest_sources(payload)

        payload["links"][0]["source"] = unicode_source_at_limit  # type: ignore[index]
        unicode_link_target = BUILDER._relative_managed_link_target(
            PurePosixPath(unicode_source_at_limit),
            PurePosixPath(target),
            owner,
        )
        self.assertLess(
            len(unicode_link_target),
            BUILDER.MAX_MANAGED_LINK_TARGET_BYTES,
        )
        self.assertEqual(
            len(unicode_link_target.encode("utf-8")),
            BUILDER.MAX_MANAGED_LINK_TARGET_BYTES,
        )
        self.assertEqual(
            BUILDER._manifest_sources(payload),
            [Path(unicode_source_at_limit)],
        )

        payload["links"][0]["source"] = unicode_source_over_limit  # type: ignore[index]
        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "managed symlink target exceeds 1023 UTF-8 bytes",
        ):
            BUILDER._manifest_sources(payload)

    def test_manifest_active_link_limit_reserves_current_transaction_record(
        self,
    ) -> None:
        self.assertEqual(
            BUILDER.MAX_MANIFEST_ACTIVE_LINKS,
            min(
                BUILDER.MAX_PENDING_LINK_RECORDS,
                BUILDER.MAX_PENDING_LINK_CLAIMS,
            )
            - 1,
        )
        self.assertEqual(BUILDER.MAX_MANIFEST_ACTIVE_LINKS, 9_999)

        def manifest(link_count: int) -> dict[str, object]:
            return {
                "version": 1,
                "links": [
                    {
                        "source": f"payload-{index}",
                        "target": f"skills/example-{index}",
                        "kind": "skill",
                    }
                    for index in range(link_count)
                ],
            }

        with mock.patch.object(BUILDER, "MAX_MANIFEST_ACTIVE_LINKS", 2):
            self.assertEqual(
                BUILDER._manifest_sources(manifest(2)),
                [Path("payload-0"), Path("payload-1")],
            )
            with self.assertRaisesRegex(
                BUILDER.PackageError,
                "active links exceed runtime transaction limit: 3 > 2",
            ):
                BUILDER._manifest_sources(manifest(3))

    def test_manifest_target_paths_enforce_byte_and_depth_limits(self) -> None:
        boundary = "/".join(["é" * 31] * 63 + ["é" * 63 + "x"])
        byte_overflow = boundary + "x"
        component_boundary = "é" * 127 + "x"
        component_overflow = component_boundary + "x"
        depth_overflow = "/".join(
            "x" for _ in range(BUILDER.MAX_MANIFEST_TARGET_PATH_DEPTH + 1)
        )
        self.assertEqual(
            len(boundary.encode("utf-8")),
            BUILDER.MAX_MANIFEST_TARGET_PATH_BYTES,
        )
        self.assertEqual(
            len(Path(boundary).parts),
            BUILDER.MAX_MANIFEST_TARGET_PATH_DEPTH,
        )
        self.assertEqual(
            len(component_boundary.encode("utf-8")),
            BUILDER.MAX_MANIFEST_TARGET_COMPONENT_BYTES,
        )

        def payload(route: str, target: str) -> dict[str, object]:
            data: dict[str, object] = {
                "version": 1,
                "links": [
                    {
                        "source": "payload",
                        "target": "skills/example",
                        "kind": "skill",
                    }
                ],
            }
            if route == "active":
                data["links"][0]["target"] = target  # type: ignore[index]
                return data
            removed_link: dict[str, object] = {
                "id": "retired",
                "source": "retired/source",
                "target": "skills/retired",
                "kind": "skill",
            }
            if route == "removed":
                removed_link["target"] = target
            else:
                removed_link["replacement_target"] = target
                data["links"][0]["target"] = target  # type: ignore[index]
            data["removed_links"] = [removed_link]
            return data

        for route in ("active", "removed", "replacement"):
            with self.subTest(route=route, limit="boundary"):
                self.assertEqual(
                    BUILDER._manifest_sources(payload(route, boundary)),
                    [Path("payload")],
                )
            with self.subTest(route=route, limit="bytes"):
                with self.assertRaisesRegex(BUILDER.PackageError, "UTF-8 bytes"):
                    BUILDER._manifest_sources(payload(route, byte_overflow))
            with self.subTest(route=route, limit="component-boundary"):
                self.assertEqual(
                    BUILDER._manifest_sources(payload(route, component_boundary)),
                    [Path("payload")],
                )
            with self.subTest(route=route, limit="component"):
                with self.assertRaisesRegex(BUILDER.PackageError, "component 1"):
                    BUILDER._manifest_sources(payload(route, component_overflow))
            with self.subTest(route=route, limit="depth"):
                with self.assertRaisesRegex(BUILDER.PackageError, "path components"):
                    BUILDER._manifest_sources(payload(route, depth_overflow))

        for label, target, message in (
            ("bytes", byte_overflow, "UTF-8 bytes"),
            ("component", component_overflow, "component 1"),
            ("depth", depth_overflow, "path components"),
        ):
            with (
                self.subTest(early_rejection=label),
                mock.patch.object(
                    BUILDER,
                    "_portable_manifest_path_key",
                ) as portable_key,
                self.assertRaisesRegex(BUILDER.PackageError, message),
            ):
                BUILDER._validate_manifest_target_path(target, "target")
            portable_key.assert_not_called()

    def test_active_link_limit_fails_before_staging_or_archive_output(self) -> None:
        repo = self.root / "repo-active-link-limit"
        links: list[dict[str, str]] = []
        for index in range(2):
            source = repo / f"payload-{index}" / "SKILL.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Example\n", encoding="utf-8")
            links.append(
                {
                    "source": f"payload-{index}",
                    "target": f"skills/example-{index}",
                    "kind": "skill",
                }
            )
        manifest_path = Path("manifest.json")
        (repo / manifest_path).write_text(
            json.dumps({"version": 1, "links": links}) + "\n",
            encoding="utf-8",
        )
        output_dir = self.root / "active-link-limit-dist"
        archive_path = output_dir / f"personal-codex-{'a' * 40}.tar.gz"

        with (
            mock.patch.object(BUILDER, "MAX_MANIFEST_ACTIVE_LINKS", 1),
            mock.patch.object(
                BUILDER,
                "_copy_source",
                side_effect=AssertionError("copy must not run"),
            ) as copy_source,
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "active links exceed runtime transaction limit",
            ),
        ):
            BUILDER.build_package(
                repo,
                manifest_path,
                output_dir,
                "a" * 40,
            )

        copy_source.assert_not_called()
        self.assertFalse(output_dir.exists())
        self.assertFalse(archive_path.exists())
        self.assertFalse(BUILDER._checksum_path(archive_path).exists())

    def test_manifest_removed_links_structure_matches_runtime_semantics(
        self,
    ) -> None:
        def removed_link(**updates: object) -> dict[str, object]:
            payload: dict[str, object] = {
                "id": "retired-skill",
                "source": "retired/source",
                "target": "skills/retired",
                "kind": "skill",
            }
            payload.update(updates)
            return payload

        def manifest(removed_links: object) -> dict[str, object]:
            return {
                "version": 1,
                "links": [
                    {
                        "source": "payload",
                        "target": "skills/example",
                        "kind": "skill",
                    }
                ],
                "removed_links": removed_links,
            }

        missing_id = removed_link()
        del missing_id["id"]
        overlong_retirement_owner = "o" * (
            BUILDER.MAX_OWNER_COMPONENT_BYTES + 1
        )
        invalid_cases = (
            ("removed-links-not-list", manifest({}), "must be a list"),
            ("removed-link-not-object", manifest(["retired"]), "must be objects"),
            ("missing-id", manifest([missing_id]), "id has unsupported characters"),
            (
                "invalid-id",
                manifest([removed_link(id="invalid/id")]),
                "id has unsupported characters",
            ),
            (
                "duplicate-id",
                manifest([removed_link(), removed_link(source="other/source")]),
                "duplicate removed link id",
            ),
            (
                "unknown-field",
                manifest([removed_link(future_field=True)]),
                "unsupported field",
            ),
            (
                "retire-key-missing-owner",
                manifest([removed_link(retires_replacements=["retired-skill"])]),
                "owner:id string",
            ),
            (
                "retire-key-invalid-owner",
                manifest(
                    [removed_link(retires_replacements=["private/invalid:retired"])]
                ),
                "owner id",
            ),
            (
                "retire-key-invalid-id",
                manifest([removed_link(retires_replacements=["private:invalid/id"])]),
                "owner:id string",
            ),
            (
                "retire-key-overlong-owner",
                manifest(
                    [
                        removed_link(
                            retires_replacements=[
                                f"{overlong_retirement_owner}:retired"
                            ]
                        )
                    ]
                ),
                "must not exceed 255",
            ),
            (
                "retire-list-type",
                manifest([removed_link(retires_replacements="private:retired")]),
                "retires_replacements must be a list",
            ),
            (
                "duplicate-retire-key",
                manifest(
                    [
                        removed_link(
                            retires_replacements=[
                                "private:retired",
                                "private:retired",
                            ]
                        )
                    ]
                ),
                "duplicate retires_replacements entries",
            ),
            (
                "legacy-type",
                manifest([removed_link(legacy="true")]),
                "legacy must be boolean",
            ),
        )
        for name, payload, message in invalid_cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(BUILDER.PackageError, message):
                    BUILDER._manifest_sources(payload)

        valid = manifest(
            [
                removed_link(
                    replacement_target="skills/example",
                    retires_replacements=["private:older-skill"],
                    legacy=True,
                )
            ]
        )
        self.assertEqual(BUILDER._manifest_sources(valid), [Path("payload")])

    def test_manifest_rejects_active_historical_target_hierarchy(self) -> None:
        def manifest(
            active_target: str,
            historical_target: str,
        ) -> dict[str, object]:
            return {
                "version": 1,
                "owner": "private",
                "links": [
                    {
                        "source": "payload",
                        "target": active_target,
                        "kind": "skill",
                    }
                ],
                "removed_links": [
                    {
                        "id": "retired",
                        "source": "retired/source",
                        "target": historical_target,
                        "kind": "skill",
                    }
                ],
            }

        for active_target, historical_target in (
            ("skills/example", "skills/example/child"),
            ("skills/example/child", "skills/example"),
            (
                "Skills/Cafe\N{COMBINING ACUTE ACCENT}",
                "skills/caf\N{LATIN SMALL LETTER E WITH ACUTE}/child",
            ),
            (
                "skills/caf\N{LATIN SMALL LETTER E WITH ACUTE}/child",
                "Skills/Cafe\N{COMBINING ACUTE ACCENT}",
            ),
        ):
            with self.subTest(
                active_target=active_target,
                historical_target=historical_target,
            ), self.assertRaisesRegex(
                BUILDER.PackageError,
                "hierarchy changes are not supported",
            ):
                BUILDER._manifest_sources(
                    manifest(active_target, historical_target)
                )

        exact_target = "skills/example"
        self.assertEqual(
            BUILDER._manifest_sources(manifest(exact_target, exact_target)),
            [Path("payload")],
        )

    def test_manifest_rejects_historical_target_hierarchy(self) -> None:
        def manifest(*historical_targets: str) -> dict[str, object]:
            return {
                "version": 1,
                "owner": "private",
                "links": [
                    {
                        "source": "payload",
                        "target": "skills/current",
                        "kind": "skill",
                    }
                ],
                "removed_links": [
                    {
                        "id": f"retired-{index}",
                        "source": f"retired/source-{index}",
                        "target": target,
                        "kind": "skill",
                    }
                    for index, target in enumerate(historical_targets)
                ],
            }

        for historical_targets in (
            ("skills/example", "skills/example/child"),
            ("skills/example/child", "skills/example"),
            (
                "Skills/Cafe\N{COMBINING ACUTE ACCENT}",
                "skills/caf\N{LATIN SMALL LETTER E WITH ACUTE}/child",
            ),
        ):
            with self.subTest(
                historical_targets=historical_targets
            ), self.assertRaisesRegex(
                BUILDER.PackageError,
                "historical manifest targets must not overlap",
            ):
                BUILDER._manifest_sources(manifest(*historical_targets))

        self.assertEqual(
            BUILDER._manifest_sources(
                manifest("skills/example", "skills/example", "skills/sibling")
            ),
            [Path("payload")],
        )

    def test_public_active_replacement_obligation_matches_runtime_semantics(
        self,
    ) -> None:
        def removed_link(
            removed_id: str,
            target: str,
            *,
            replacement_target: str | None = None,
            retires_replacements: list[str] | None = None,
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "id": removed_id,
                "source": f"retired/{removed_id}",
                "target": target,
                "kind": "skill",
            }
            if replacement_target is not None:
                payload["replacement_target"] = replacement_target
            if retires_replacements is not None:
                payload["retires_replacements"] = retires_replacements
            return payload

        def manifest(
            *targets: str,
            removed_links: list[dict[str, object]],
            owner: str = BUILDER.PUBLIC_OWNER,
        ) -> dict[str, object]:
            return {
                "version": 1,
                "owner": owner,
                "links": [
                    {
                        "source": f"payload/{index}",
                        "target": target,
                        "kind": "skill",
                    }
                    for index, target in enumerate(targets)
                ],
                "removed_links": removed_links,
            }

        migration = removed_link(
            "move-old",
            "skills/old",
            replacement_target="skills/replacement",
        )
        unavailable = manifest("skills/keep", removed_links=[migration])
        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "replacement target skills/replacement is unavailable",
        ):
            BUILDER._manifest_sources(unavailable)

        active = manifest(
            "skills/keep",
            "skills/replacement",
            removed_links=[migration],
        )
        self.assertEqual(
            BUILDER._manifest_sources(active),
            [Path("payload/0"), Path("payload/1")],
        )

        retirement = removed_link(
            "remove-replacement",
            "skills/replacement",
            retires_replacements=["public:move-old"],
        )
        retired = manifest(
            "skills/keep",
            removed_links=[migration, retirement],
        )
        self.assertEqual(
            BUILDER._manifest_sources(retired),
            [Path("payload/0")],
        )

        private_migration = manifest(
            "skills/keep",
            removed_links=[migration],
            owner="private",
        )
        self.assertEqual(
            BUILDER._manifest_sources(private_migration),
            [Path("payload/0")],
        )

    def test_manifest_removed_link_retirement_graph_matches_runtime_semantics(
        self,
    ) -> None:
        def removed_link(
            removed_id: str,
            target: str,
            *,
            replacement_target: str | None = None,
            retires_replacements: list[str] | None = None,
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "id": removed_id,
                "source": f"retired/{removed_id}",
                "target": target,
                "kind": "skill",
            }
            if replacement_target is not None:
                payload["replacement_target"] = replacement_target
            if retires_replacements is not None:
                payload["retires_replacements"] = retires_replacements
            return payload

        def manifest(removed_links: list[dict[str, object]]) -> dict[str, object]:
            return {
                "version": 1,
                "links": [
                    {
                        "source": "payload",
                        "target": "skills/example",
                        "kind": "skill",
                    }
                ],
                "removed_links": removed_links,
            }

        invalid_cases = (
            (
                "self-retirement",
                manifest(
                    [
                        removed_link(
                            "self",
                            "skills/self",
                            retires_replacements=["public:self"],
                        )
                    ]
                ),
                "cannot retire itself",
            ),
            (
                "unknown-same-owner-retirement",
                manifest(
                    [
                        removed_link(
                            "current",
                            "skills/current",
                            retires_replacements=["public:missing"],
                        )
                    ]
                ),
                "retires unknown replacement public:missing",
            ),
            (
                "replacement-target-mismatch",
                manifest(
                    [
                        removed_link(
                            "old",
                            "skills/old",
                            replacement_target="skills/replacement",
                        ),
                        removed_link(
                            "current",
                            "skills/other",
                            retires_replacements=["public:old"],
                        ),
                    ]
                ),
                "target does not match replacement for public:old",
            ),
            (
                "retirement-cycle",
                manifest(
                    [
                        removed_link(
                            "a",
                            "skills/a",
                            replacement_target="skills/b",
                            retires_replacements=["public:b"],
                        ),
                        removed_link(
                            "b",
                            "skills/b",
                            replacement_target="skills/a",
                            retires_replacements=["public:a"],
                        ),
                    ]
                ),
                "replacement retirement cycle detected",
            ),
        )

        for name, payload, message in invalid_cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(BUILDER.PackageError, message):
                    BUILDER._manifest_sources(payload)

    def test_retirement_graph_failure_precedes_staging_copy(self) -> None:
        repo = self.root / "repo-retirement-graph"
        source = repo / "payload" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Example\n", encoding="utf-8")
        manifest = {
            "version": 1,
            "links": [
                {
                    "source": "payload",
                    "target": "skills/example",
                    "kind": "skill",
                }
            ],
            "removed_links": [
                {
                    "id": "retired",
                    "source": "retired/source",
                    "target": "skills/retired",
                    "kind": "skill",
                    "retires_replacements": ["public:retired"],
                }
            ],
        }
        manifest_path = Path("manifest.json")
        (repo / manifest_path).write_text(
            json.dumps(manifest) + "\n",
            encoding="utf-8",
        )
        snapshot = BUILDER.StrictReleaseSnapshot(
            manifest=manifest,
            manifest_mode=b"100644",
            directories=(Path("payload"),),
            files=(
                BUILDER._SnapshotFile(
                    path=Path("payload/SKILL.md"),
                    mode=b"100644",
                    object_id=b"a" * 40,
                ),
            ),
        )

        staging = self.root / "staging-live-retirement-graph"
        staging.mkdir()
        with (
            mock.patch.object(BUILDER, "_copy_source") as copy_source,
            self.assertRaisesRegex(BUILDER.PackageError, "cannot retire itself"),
        ):
            BUILDER.stage_release(repo, manifest_path, staging)
        copy_source.assert_not_called()
        self.assertEqual(list(staging.iterdir()), [])

        staging = self.root / "staging-strict-retirement-graph"
        staging.mkdir()
        with (
            mock.patch.object(BUILDER, "_copy_git_blob") as copy_git_blob,
            self.assertRaisesRegex(BUILDER.PackageError, "cannot retire itself"),
        ):
            BUILDER.stage_strict_release(repo, snapshot, staging)
        copy_git_blob.assert_not_called()
        self.assertEqual(list(staging.iterdir()), [])

    def test_manifest_base_release_structure_matches_runtime_semantics(
        self,
    ) -> None:
        def manifest(base_release: object) -> dict[str, object]:
            return {
                "version": 1,
                "links": [
                    {
                        "source": "payload",
                        "target": "skills/example",
                        "kind": "skill",
                    }
                ],
                "base_release": base_release,
            }

        invalid_cases = (
            ("not-object", manifest([]), "must be an object"),
            (
                "unknown-field",
                manifest(
                    {
                        "repo": "Joey-Tools/codex-toolbox",
                        "shaa": "a" * 40,
                    }
                ),
                r"unsupported field\(s\): shaa",
            ),
            (
                "bad-repo",
                manifest({"repo": "codex-toolbox"}),
                "owner/repo string",
            ),
            (
                "extra-repo-component",
                manifest({"repo": "owner/repo/extra"}),
                "owner/repo string",
            ),
            (
                "empty-repo-components",
                manifest({"repo": "/"}),
                "owner/repo string",
            ),
            (
                "leading-repo-punctuation",
                manifest({"repo": ".owner/repo"}),
                "owner/repo string",
            ),
            (
                "bad-sha",
                manifest({"sha": "a" * 39}),
                "40-character lowercase hex SHA",
            ),
            (
                "uppercase-sha",
                manifest({"sha": "A" * 40}),
                "40-character lowercase hex SHA",
            ),
        )
        for name, payload, message in invalid_cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(BUILDER.PackageError, message):
                    BUILDER._manifest_sources(payload)

        valid_cases = (
            ("null", manifest(None)),
            (
                "object",
                manifest(
                    {
                        "repo": "Joey-Tools/codex-toolbox",
                        "sha": "a" * 40,
                    }
                ),
            ),
        )
        for name, payload in valid_cases:
            with self.subTest(name=name):
                self.assertEqual(
                    BUILDER._manifest_sources(payload),
                    [Path("payload")],
                )

    def test_manifest_rejects_reserved_targets_and_descendants_everywhere(
        self,
    ) -> None:
        def manifest(field: str, target: str) -> dict[str, object]:
            payload: dict[str, object] = {
                "version": 1,
                "links": [
                    {
                        "source": "payload",
                        "target": "skills/example",
                        "kind": "skill",
                    }
                ],
            }
            if field == "active":
                payload["links"][0]["target"] = target  # type: ignore[index]
            else:
                removed: dict[str, object] = {
                    "id": "retired",
                    "source": "retired/source",
                    "target": "skills/retired",
                    "kind": "skill",
                }
                removed[field] = target
                payload["removed_links"] = [removed]
            return payload

        for root in (
            "personal-sync",
            ".personal-sync-pending-transaction.json",
        ):
            for field in ("active", "target", "replacement_target"):
                for suffix in ("", "/child"):
                    with self.subTest(root=root, field=field, suffix=suffix):
                        with self.assertRaisesRegex(
                            BUILDER.PackageError,
                            "reserved personal sync path",
                        ):
                            BUILDER._manifest_sources(
                                manifest(field, root + suffix)
                            )

        portable_spelling = manifest(
            "active",
            ".PERSONAL-SYNC-PENDING-TRANSACTION.JSON/child",
        )
        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "reserved personal sync path",
        ):
            BUILDER._manifest_sources(portable_spelling)

    def test_strict_snapshot_validates_source_kind_from_commit_inventory(
        self,
    ) -> None:
        cases = (
            ("directory-as-file", "payload", "file", "file source is missing"),
            (
                "file-as-directory",
                "payload/SKILL.md",
                "directory",
                "directory source is missing",
            ),
        )
        for name, source_path, kind, message in cases:
            with self.subTest(name=name):
                repo, manifest_path, _source, _head = self.init_tracked_repo(name)
                payload = json.loads((repo / manifest_path).read_text(encoding="utf-8"))
                payload["links"][0]["source"] = source_path
                payload["links"][0]["kind"] = kind
                (repo / manifest_path).write_text(
                    json.dumps(payload) + "\n",
                    encoding="utf-8",
                )
                self.git(repo, "add", manifest_path.as_posix())
                head = self.commit(repo, f"Set {name} manifest kind")
                with (
                    mock.patch.object(
                        BUILDER,
                        "_copy_git_blob",
                        side_effect=AssertionError("copy must not run"),
                    ) as copy_blob,
                    self.assertRaisesRegex(BUILDER.PackageError, message),
                ):
                    self.strict_build(repo, manifest_path, head)
                copy_blob.assert_not_called()

    def test_strict_snapshot_requires_skill_md_from_commit_inventory(self) -> None:
        repo = self.root / "missing-skill-entrypoint"
        source = repo / "payload" / "README.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Example\n", encoding="utf-8")
        manifest_path = self.write_manifest(repo, "payload")
        self.git(repo, "init", "-q")
        self.git(repo, "add", manifest_path.as_posix(), source.relative_to(repo).as_posix())
        head = self.commit(repo, "Add skill without entrypoint")

        with (
            mock.patch.object(
                BUILDER,
                "_copy_git_blob",
                side_effect=AssertionError("copy must not run"),
            ) as copy_blob,
            self.assertRaisesRegex(BUILDER.PackageError, "missing SKILL.md"),
        ):
            self.strict_build(repo, manifest_path, head)
        copy_blob.assert_not_called()

    def test_strict_archive_limits_fail_before_blob_copy(self) -> None:
        repo, manifest, source, _head = self.init_tracked_repo("archive-limits")
        source.write_bytes(b"x" * 4096)
        self.git(repo, "add", source.relative_to(repo).as_posix())
        head = self.commit(repo, "Enlarge package input")
        package_name = f"personal-codex-{head}"
        cases = (
            ("members", "MAX_ARCHIVE_MEMBERS", 4, "member limit"),
            ("single-file", "MAX_ARCHIVE_MEMBER_BYTES", 1024, "exceeds 1024 bytes"),
            (
                "expanded-files",
                "MAX_ARCHIVE_EXPANDED_BYTES",
                4100,
                "expanded file byte limit",
            ),
            (
                "root-prefixed-path",
                "MAX_ARCHIVE_MEMBER_PATH_BYTES",
                len(package_name.encode("utf-8")),
                "member path exceeds",
            ),
            (
                "component",
                "MAX_ARCHIVE_MEMBER_COMPONENT_BYTES",
                len(package_name.encode("utf-8")) - 1,
                "component exceeds",
            ),
            ("depth", "MAX_ARCHIVE_MEMBER_PATH_DEPTH", 1, "depth limit"),
            (
                "tar-stream",
                "MAX_ARCHIVE_EXPANDED_BYTES",
                8192,
                "tar stream limit",
            ),
        )

        for name, constant, limit, message in cases:
            with self.subTest(name=name):
                with (
                    mock.patch.object(BUILDER, constant, limit),
                    mock.patch.object(
                        BUILDER,
                        "_copy_git_blob",
                        side_effect=AssertionError("copy must not run"),
                    ) as copy_blob,
                    self.assertRaisesRegex(BUILDER.PackageError, message),
                ):
                    self.strict_build(repo, manifest, head)
                copy_blob.assert_not_called()

    def test_default_archive_format_limits_accept_exact_boundaries(self) -> None:
        repo, manifest, source, head = self.init_tracked_repo(
            "default-archive-boundaries"
        )
        source.write_bytes(b"x" * 4096)
        package_name = f"personal-codex-{head}"
        member_names = (
            package_name,
            f"{package_name}/payload",
            f"{package_name}/payload/SKILL.md",
            f"{package_name}/personal_codex",
            f"{package_name}/personal_codex/sync-manifest.json",
        )
        maximum_path_bytes = max(
            len(member_name.encode("utf-8")) for member_name in member_names
        )
        maximum_component_bytes = max(
            len(component.encode("utf-8"))
            for member_name in member_names
            for component in member_name.split("/")
        )
        output_dir = self.root / "default-boundary-dist"

        with (
            mock.patch.object(BUILDER, "MAX_ARCHIVE_MEMBERS", len(member_names)),
            mock.patch.object(BUILDER, "MAX_ARCHIVE_MEMBER_BYTES", 4096),
            mock.patch.object(
                BUILDER,
                "MAX_ARCHIVE_MEMBER_PATH_BYTES",
                maximum_path_bytes,
            ),
            mock.patch.object(
                BUILDER,
                "MAX_ARCHIVE_MEMBER_COMPONENT_BYTES",
                maximum_component_bytes,
            ),
            mock.patch.object(BUILDER, "MAX_ARCHIVE_MEMBER_PATH_DEPTH", 3),
        ):
            archive, checksum = BUILDER.build_package(
                repo,
                manifest,
                output_dir,
                head,
            )

        self.assertTrue(archive.is_file())
        self.assertTrue(checksum.is_file())
        with tarfile.open(archive, "r:gz") as package:
            self.assertEqual(len(package.getmembers()), len(member_names))

        expanded_stream_bytes = len(gzip.decompress(archive.read_bytes()))
        exact_expanded_dir = self.root / "default-expanded-boundary-dist"
        with mock.patch.object(
            BUILDER,
            "MAX_ARCHIVE_EXPANDED_BYTES",
            expanded_stream_bytes,
        ):
            exact_archive, exact_checksum = BUILDER.build_package(
                repo,
                manifest,
                exact_expanded_dir,
                head,
            )
        self.assertTrue(exact_archive.is_file())
        self.assertTrue(exact_checksum.is_file())

    def test_default_archive_format_failures_precede_output_creation(self) -> None:
        repo, manifest, source, head = self.init_tracked_repo(
            "default-archive-limits"
        )
        source.write_bytes(b"x" * 4096)
        package_name = f"personal-codex-{head}"
        cases = (
            ("members", "MAX_ARCHIVE_MEMBERS", 4, "member limit"),
            (
                "single-file",
                "MAX_ARCHIVE_MEMBER_BYTES",
                1024,
                "member exceeds expanded byte limit",
            ),
            (
                "expanded-files",
                "MAX_ARCHIVE_EXPANDED_BYTES",
                4100,
                "expanded file byte limit",
            ),
            (
                "root-prefixed-path",
                "MAX_ARCHIVE_MEMBER_PATH_BYTES",
                len(package_name.encode("utf-8")),
                "member path exceeds",
            ),
            (
                "component",
                "MAX_ARCHIVE_MEMBER_COMPONENT_BYTES",
                len(package_name.encode("utf-8")) - 1,
                "component exceeds",
            ),
            ("depth", "MAX_ARCHIVE_MEMBER_PATH_DEPTH", 1, "depth limit"),
            (
                "tar-stream",
                "MAX_ARCHIVE_EXPANDED_BYTES",
                8192,
                "tar stream limit",
            ),
        )

        for name, constant, limit, message in cases:
            with self.subTest(name=name):
                output_dir = self.root / f"default-{name}-dist"
                archive_path = output_dir / f"{package_name}.tar.gz"
                with (
                    mock.patch.object(BUILDER, constant, limit),
                    self.assertRaisesRegex(BUILDER.PackageError, message),
                ):
                    BUILDER.build_package(
                        repo,
                        manifest,
                        output_dir,
                        head,
                    )
                self.assertFalse(output_dir.exists())
                self.assertFalse(archive_path.exists())
                self.assertFalse(BUILDER._checksum_path(archive_path).exists())

        output_dir = self.root / "default-portable-conflict-dist"
        archive_path = output_dir / f"{package_name}.tar.gz"
        with (
            mock.patch.object(
                BUILDER,
                "_staged_release_inventory",
                return_value=({Path("Payload"), Path("payload")}, {}),
            ) as staged_inventory,
            self.assertRaisesRegex(BUILDER.PackageError, "portable path conflict"),
        ):
            BUILDER.build_package(
                repo,
                manifest,
                output_dir,
                head,
            )
        staged_inventory.assert_called_once()
        self.assertFalse(output_dir.exists())
        self.assertFalse(archive_path.exists())
        self.assertFalse(BUILDER._checksum_path(archive_path).exists())

    def test_archive_writer_bounds_uncompressed_stream_and_removes_partial_file(
        self,
    ) -> None:
        staging = self.root / "bounded-archive-staging"
        staging.mkdir()
        (staging / "payload.txt").write_text("payload\n", encoding="utf-8")
        archive = self.root / "bounded.tar.gz"

        with (
            mock.patch.object(BUILDER, "MAX_ARCHIVE_EXPANDED_BYTES", 8192),
            self.assertRaisesRegex(BUILDER.PackageError, "tar stream limit"),
        ):
            BUILDER.create_archive(staging, archive, "personal-codex-test")

        self.assertFalse(archive.exists())

    def test_archive_writer_bounds_compressed_stream_and_removes_partial_file(
        self,
    ) -> None:
        staging = self.root / "bounded-compressed-staging"
        staging.mkdir()
        (staging / "payload.txt").write_text("payload\n", encoding="utf-8")
        archive = self.root / "bounded-compressed.tar.gz"

        with (
            mock.patch.object(BUILDER, "MAX_ARCHIVE_COMPRESSED_BYTES", 1),
            self.assertRaisesRegex(BUILDER.PackageError, "compressed size limit"),
        ):
            BUILDER.create_archive(staging, archive, "personal-codex-test")

        self.assertFalse(archive.exists())

    def test_create_archive_preserves_preexisting_read_only_output(self) -> None:
        staging = self.root / "existing-archive-staging"
        staging.mkdir()
        (staging / "payload.txt").write_text("payload\n", encoding="utf-8")
        archive = self.root / "existing.tar.gz"
        archive.write_bytes(b"existing archive\n")
        archive.chmod(0o444)
        original_identity = (archive.stat().st_dev, archive.stat().st_ino)

        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "refusing to overwrite package output",
        ):
            BUILDER.create_archive(staging, archive, "personal-codex-test")

        self.assertEqual(archive.read_bytes(), b"existing archive\n")
        self.assertEqual(
            (archive.stat().st_dev, archive.stat().st_ino),
            original_identity,
        )

    def test_checksum_failure_removes_archive_and_partial_sidecar(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-checksum-failure"
        )
        output_dir = self.root / "checksum-failure-dist"
        archive = output_dir / f"personal-codex-{head}.tar.gz"
        checksum = BUILDER._checksum_path(archive)

        def fail_checksum(
            _archive_fd: int,
            checksum_fd: int,
            archive_name: str,
        ) -> None:
            self.assertEqual(archive_name, archive.name)
            os.write(checksum_fd, b"partial\n")
            raise OSError("injected checksum failure")

        with (
            mock.patch.object(
                BUILDER,
                "_write_checksum_to_fd",
                side_effect=fail_checksum,
            ),
            self.assertRaisesRegex(OSError, "injected checksum failure"),
        ):
            BUILDER.build_package(
                repo,
                manifest,
                output_dir,
                head,
            )

        self.assertFalse(archive.exists())
        self.assertFalse(checksum.exists())
        self.assertEqual(list(output_dir.iterdir()), [])

    def test_checksum_publish_collision_rolls_back_only_current_archive(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-checksum-publish-collision"
        )
        output_dir = self.root / "checksum-publish-collision-dist"
        archive = output_dir / f"personal-codex-{head}.tar.gz"
        checksum = BUILDER._checksum_path(archive)
        real_rename_noreplace = BUILDER._rename_noreplace_at
        injected = False

        def collide_with_checksum(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal injected
            if destination_name == checksum.name and not injected:
                injected = True
                foreign_fd = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_parent_fd,
                )
                try:
                    os.write(foreign_fd, b"existing checksum\n")
                finally:
                    os.close(foreign_fd)
            real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                BUILDER,
                "_rename_noreplace_at",
                side_effect=collide_with_checksum,
            ),
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "differs from generated content",
            ),
        ):
            BUILDER.build_package(repo, manifest, output_dir, head)

        self.assertFalse(archive.exists())
        self.assertEqual(checksum.read_bytes(), b"existing checksum\n")
        self.assertEqual(list(output_dir.iterdir()), [checksum])

    def test_cleanup_preserves_raced_archive_replacement(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-raced-archive-cleanup"
        )
        output_dir = self.root / "raced-archive-cleanup-dist"
        archive = output_dir / f"personal-codex-{head}.tar.gz"
        checksum = BUILDER._checksum_path(archive)
        real_rename_noreplace = BUILDER._rename_noreplace_at
        archive_replaced = False
        checksum_created = False

        def replace_archive_before_cleanup(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal archive_replaced, checksum_created
            if destination_name == checksum.name and not archive_replaced:
                os.unlink(archive.name, dir_fd=destination_parent_fd)
                foreign_fd = os.open(
                    archive.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_parent_fd,
                )
                try:
                    os.write(foreign_fd, b"foreign archive\n")
                finally:
                    os.close(foreign_fd)
                archive_replaced = True
            if destination_name == checksum.name and not checksum_created:
                foreign_fd = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_parent_fd,
                )
                try:
                    os.write(foreign_fd, b"foreign checksum\n")
                finally:
                    os.close(foreign_fd)
                checksum_created = True
            real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                BUILDER,
                "_rename_noreplace_at",
                side_effect=replace_archive_before_cleanup,
            ),
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "package output cleanup was incomplete",
            ),
        ):
            BUILDER.build_package(repo, manifest, output_dir, head)

        self.assertEqual(archive.read_bytes(), b"foreign archive\n")
        self.assertEqual(checksum.read_bytes(), b"foreign checksum\n")
        self.assertEqual(set(output_dir.iterdir()), {archive, checksum})

    def test_cleanup_restores_replacement_raced_at_isolation(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-raced-isolation-cleanup"
        )
        output_dir = self.root / "raced-isolation-cleanup-dist"
        archive = output_dir / f"personal-codex-{head}.tar.gz"
        checksum = BUILDER._checksum_path(archive)
        real_rename_noreplace = BUILDER._rename_noreplace_at
        archive_replaced = False
        checksum_created = False

        def replace_archive_at_isolation(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal archive_replaced, checksum_created
            if destination_name == checksum.name and not checksum_created:
                foreign_fd = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_parent_fd,
                )
                try:
                    os.write(foreign_fd, b"foreign checksum\n")
                finally:
                    os.close(foreign_fd)
                checksum_created = True
            if (
                source_name == archive.name
                and destination_name.startswith(".personal-codex-retained-")
                and not archive_replaced
            ):
                os.unlink(source_name, dir_fd=source_parent_fd)
                foreign_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(foreign_fd, b"foreign archive\n")
                finally:
                    os.close(foreign_fd)
                archive_replaced = True
            real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                BUILDER,
                "_rename_noreplace_at",
                side_effect=replace_archive_at_isolation,
            ),
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "package output cleanup was incomplete",
            ),
        ):
            BUILDER.build_package(repo, manifest, output_dir, head)

        self.assertEqual(archive.read_bytes(), b"foreign archive\n")
        self.assertEqual(checksum.read_bytes(), b"foreign checksum\n")
        self.assertEqual(set(output_dir.iterdir()), {archive, checksum})

    def test_build_resumes_after_archive_only_publication(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-resume-archive-only"
        )
        reference_dir = self.root / "resume-reference-dist"
        reference_archive, _reference_checksum = BUILDER.build_package(
            repo,
            manifest,
            reference_dir,
            head,
        )
        output_dir = self.root / "resume-archive-only-dist"
        output_dir.mkdir()
        archive = output_dir / reference_archive.name
        archive.write_bytes(reference_archive.read_bytes())
        archive_identity = (archive.stat().st_dev, archive.stat().st_ino)

        resumed_archive, resumed_checksum = BUILDER.build_package(
            repo,
            manifest,
            output_dir,
            head,
        )

        self.assertEqual(resumed_archive, archive)
        self.assertEqual(
            (archive.stat().st_dev, archive.stat().st_ino),
            archive_identity,
        )
        expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(
            resumed_checksum.read_text(encoding="utf-8"),
            f"{expected_digest}  {archive.name}\n",
        )
        checksum_identity = (
            resumed_checksum.stat().st_dev,
            resumed_checksum.stat().st_ino,
        )

        BUILDER.build_package(repo, manifest, output_dir, head)

        self.assertEqual(
            (archive.stat().st_dev, archive.stat().st_ino),
            archive_identity,
        )
        self.assertEqual(
            (resumed_checksum.stat().st_dev, resumed_checksum.stat().st_ino),
            checksum_identity,
        )
        self.assertEqual(set(output_dir.iterdir()), {archive, resumed_checksum})

    def test_build_rejects_and_preserves_different_archive_residue(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-different-archive-residue"
        )
        output_dir = self.root / "different-archive-residue-dist"
        output_dir.mkdir()
        archive = output_dir / f"personal-codex-{head}.tar.gz"
        checksum = BUILDER._checksum_path(archive)
        archive.write_bytes(b"different archive\n")
        archive_identity = (archive.stat().st_dev, archive.stat().st_ino)

        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "differs from generated content",
        ):
            BUILDER.build_package(repo, manifest, output_dir, head)

        self.assertEqual(archive.read_bytes(), b"different archive\n")
        self.assertEqual(
            (archive.stat().st_dev, archive.stat().st_ino),
            archive_identity,
        )
        self.assertFalse(checksum.exists())
        self.assertEqual(list(output_dir.iterdir()), [archive])

    def test_build_rejects_hardlinked_matching_archive_residue(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-hardlinked-archive-residue"
        )
        reference_dir = self.root / "hardlinked-archive-reference-dist"
        reference_archive, _reference_checksum = BUILDER.build_package(
            repo,
            manifest,
            reference_dir,
            head,
        )
        output_dir = self.root / "hardlinked-archive-residue-dist"
        output_dir.mkdir()
        archive = output_dir / reference_archive.name
        archive.write_bytes(reference_archive.read_bytes())
        archive_alias = output_dir / "archive-alias.tar.gz"
        os.link(archive, archive_alias)
        checksum = BUILDER._checksum_path(archive)
        archive_identity = (archive.stat().st_dev, archive.stat().st_ino)

        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "unsafe link count",
        ):
            BUILDER.build_package(repo, manifest, output_dir, head)

        self.assertEqual(
            (archive.stat().st_dev, archive.stat().st_ino),
            archive_identity,
        )
        self.assertEqual(archive.read_bytes(), reference_archive.read_bytes())
        self.assertTrue(archive_alias.samefile(archive))
        self.assertFalse(checksum.exists())
        self.assertEqual(set(output_dir.iterdir()), {archive, archive_alias})

    def test_build_rejects_world_writable_matching_checksum_residue(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-writable-checksum-residue"
        )
        reference_dir = self.root / "writable-checksum-reference-dist"
        reference_archive, reference_checksum = BUILDER.build_package(
            repo,
            manifest,
            reference_dir,
            head,
        )
        output_dir = self.root / "writable-checksum-residue-dist"
        output_dir.mkdir()
        archive = output_dir / reference_archive.name
        checksum = output_dir / reference_checksum.name
        archive.write_bytes(reference_archive.read_bytes())
        checksum.write_bytes(reference_checksum.read_bytes())
        checksum.chmod(0o666)
        archive_identity = (archive.stat().st_dev, archive.stat().st_ino)
        checksum_identity = (checksum.stat().st_dev, checksum.stat().st_ino)

        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "unsafe permissions",
        ):
            BUILDER.build_package(repo, manifest, output_dir, head)

        self.assertEqual(
            (archive.stat().st_dev, archive.stat().st_ino),
            archive_identity,
        )
        self.assertEqual(
            (checksum.stat().st_dev, checksum.stat().st_ino),
            checksum_identity,
        )
        self.assertEqual(archive.read_bytes(), reference_archive.read_bytes())
        self.assertEqual(checksum.read_bytes(), reference_checksum.read_bytes())
        self.assertEqual(stat.S_IMODE(checksum.stat().st_mode), 0o666)
        self.assertEqual(set(output_dir.iterdir()), {archive, checksum})

    def test_successful_build_publishes_only_final_output_pair(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo(
            "repo-final-output-pair"
        )
        output_dir = self.root / "final-output-pair-dist"

        archive, checksum = BUILDER.build_package(
            repo,
            manifest,
            output_dir,
            head,
        )

        self.assertEqual(set(output_dir.iterdir()), {archive, checksum})
        expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(
            checksum.read_text(encoding="utf-8"),
            f"{expected_digest}  {archive.name}\n",
        )

    def test_strict_blob_copy_bounds_output_before_creating_destination(self) -> None:
        destination = self.root / "staged" / "payload.bin"
        snapshot_file = BUILDER._SnapshotFile(
            path=Path("payload.bin"),
            mode=b"100644",
            object_id=b"a" * 40,
            size=4,
        )
        oversized = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=b"12345",
            stderr=b"",
        )

        with (
            mock.patch.object(
                BUILDER,
                "_bounded_git_output",
                return_value=oversized,
            ) as bounded_output,
            self.assertRaisesRegex(BUILDER.PackageError, "declared blob size"),
        ):
            BUILDER._copy_git_blob(self.root, snapshot_file, destination)

        self.assertEqual(bounded_output.call_args.kwargs["stdout_limit"], 4)
        self.assertFalse(destination.exists())

    def test_blob_size_preflight_caches_duplicate_object_ids(self) -> None:
        object_id = b"b" * 40
        files = {
            Path("first"): BUILDER._SnapshotFile(
                Path("first"),
                b"100644",
                object_id,
            ),
            Path("second"): BUILDER._SnapshotFile(
                Path("second"),
                b"100644",
                object_id,
            ),
        }

        with mock.patch.object(
            BUILDER,
            "_git_blob_size",
            return_value=7,
        ) as blob_size:
            sized = BUILDER._bind_strict_snapshot_file_sizes(
                self.root,
                files,
                b"{}\n",
            )

        blob_size.assert_called_once()
        self.assertEqual({entry.size for entry in sized.values()}, {7})

    def test_strict_inventory_rejects_identical_duplicate_raw_paths(self) -> None:
        object_id = b"a" * 40
        index_record = b"100644 " + object_id + b" 0\tpayload/SKILL.md\0"
        tree_record = b"100644 blob " + object_id + b"\tpayload/SKILL.md\0"
        cases = (
            (
                "index",
                BUILDER._parse_index_inventory,
                index_record,
                "duplicate indexed manifest-source path",
            ),
            (
                "tree",
                BUILDER._parse_tree_inventory,
                tree_record,
                "duplicate committed manifest-source path",
            ),
        )
        for name, parser, record, message in cases:
            with self.subTest(name=name):
                result = subprocess.CompletedProcess(
                    args=["git"],
                    returncode=0,
                    stdout=record + record,
                    stderr=b"",
                )
                with self.assertRaisesRegex(BUILDER.PackageError, message):
                    parser(result)

    def test_legacy_strict_check_rejects_identical_duplicate_raw_paths(self) -> None:
        repo, manifest, _source, _head = self.init_tracked_repo()
        object_id = b"a" * 40
        record = b"100644 " + object_id + b" 0\tmanifest.json\0"
        duplicate_inventory = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=record + record,
            stderr=b"",
        )

        with (
            mock.patch.object(
                BUILDER,
                "_bounded_git_index_inventory",
                return_value=duplicate_inventory,
            ),
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "duplicate indexed manifest-source path",
            ),
        ):
            BUILDER.ensure_manifest_sources_are_strictly_tracked(
                repo,
                manifest,
            )

    def test_inventory_reader_preserves_nul_records(self) -> None:
        result = BUILDER._bounded_git_inventory(
            self.root,
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'first\\0second\\0')",
            ],
            "test inventory",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"first\0second\0")
        self.assertEqual(result.stderr, b"")

    def test_inventory_reader_terminates_and_reaps_on_stream_overflow(self) -> None:
        cases = {
            "stdout": (
                "import os, time; os.write(1, b'x' * 65536); time.sleep(5)",
                "test inventory exceeds",
            ),
            "stderr": (
                "import os, time; os.write(2, b'x' * 65536); time.sleep(5)",
                "test inventory stderr exceeds",
            ),
        }
        real_popen = BUILDER.subprocess.Popen
        for stream, (program, message) in cases.items():
            with self.subTest(stream=stream):
                captured: dict[str, subprocess.Popen[bytes]] = {}

                def capture_process(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    captured["process"] = process
                    return process

                with (
                    mock.patch.object(BUILDER, "GIT_INVENTORY_LIMIT_BYTES", 128),
                    mock.patch.object(
                        BUILDER.subprocess,
                        "Popen",
                        side_effect=capture_process,
                    ),
                    self.assertRaisesRegex(BUILDER.PackageError, message),
                ):
                    BUILDER._bounded_git_inventory(
                        self.root,
                        [sys.executable, "-c", program],
                        "test inventory",
                    )

                process = captured["process"]
                self.assertIsNotNone(process.returncode)
                self.assertIsNotNone(process.poll())

    def test_inventory_reader_reaps_process_when_selector_registration_fails(self) -> None:
        real_popen = BUILDER.subprocess.Popen
        captured: dict[str, subprocess.Popen[bytes]] = {}

        def capture_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            captured["process"] = process
            return process

        class FailingSelector:
            closed = False

            def register(self, *_args, **_kwargs) -> None:
                raise OSError("selector registration failed")

            def close(self) -> None:
                self.closed = True

        failing_selector = FailingSelector()
        with (
            mock.patch.object(
                BUILDER.subprocess,
                "Popen",
                side_effect=capture_process,
            ),
            mock.patch.object(
                BUILDER.selectors,
                "DefaultSelector",
                return_value=failing_selector,
            ),
            self.assertRaisesRegex(OSError, "selector registration failed"),
        ):
            BUILDER._bounded_git_output(
                self.root,
                [sys.executable, "-c", "import time; time.sleep(5)"],
                stdout_limit=128,
                stdout_overflow_error="stdout overflow",
                stderr_overflow_error="stderr overflow",
            )

        process = captured["process"]
        self.assertTrue(failing_selector.closed)
        self.assertIsNotNone(process.returncode)
        self.assertIsNotNone(process.poll())
        self.assertIsNotNone(process.stdout)
        self.assertIsNotNone(process.stderr)
        assert process.stdout is not None
        assert process.stderr is not None
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_committed_manifest_blob_size_is_checked_before_read(self) -> None:
        repo, manifest, _source, _head = self.init_tracked_repo()
        object_id = self.git(repo, "rev-parse", f"HEAD:{manifest.as_posix()}")
        commands: list[list[str]] = []
        real_bounded_output = BUILDER._bounded_git_output

        def record_command(repo_root: Path, args: list[str], **kwargs):
            commands.append(args)
            return real_bounded_output(repo_root, args, **kwargs)

        with (
            mock.patch.object(
                BUILDER,
                "_bounded_git_output",
                side_effect=record_command,
            ),
            self.assertRaisesRegex(BUILDER.PackageError, "exceeds 16 bytes"),
        ):
            BUILDER._read_git_blob(
                repo,
                object_id.encode("ascii"),
                f"manifest {manifest}",
                max_bytes=16,
            )

        cat_file_commands = [args for args in commands if args[1] == "cat-file"]
        self.assertEqual(cat_file_commands[0][1:3], ["cat-file", "-s"])
        self.assertEqual(len(cat_file_commands), 1)

    def test_stage_paths_reject_manifest_that_expands_past_limit(self) -> None:
        repo = self.root / "repo-expanded-manifest"
        source = repo / "payload" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Example\n", encoding="utf-8")
        manifest = {
            "version": 1,
            "links": [
                {
                    "source": "payload",
                    "target": "skills/example",
                    "kind": "skill",
                }
            ],
            "reference_only": [],
        }
        compact_payload = json.dumps(
            manifest,
            separators=(",", ":"),
        ).encode("utf-8")
        expanded_payload = (
            json.dumps(manifest, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")
        self.assertLess(len(compact_payload), len(expanded_payload))
        manifest_path = Path("manifest.json")
        (repo / manifest_path).write_bytes(compact_payload)
        snapshot = BUILDER.StrictReleaseSnapshot(
            manifest=manifest,
            manifest_mode=b"100644",
            directories=(),
            files=(),
        )
        operations = {
            "live": lambda staging: BUILDER.stage_release(
                repo,
                manifest_path,
                staging,
            ),
            "strict": lambda staging: BUILDER.stage_strict_release(
                repo,
                snapshot,
                staging,
            ),
        }

        for name, operation in operations.items():
            with self.subTest(name=name):
                staging = self.root / f"staging-{name}"
                staging.mkdir()
                with (
                    mock.patch.object(
                        BUILDER,
                        "MAX_RELEASE_MANIFEST_BYTES",
                        len(compact_payload),
                    ),
                    self.assertRaisesRegex(
                        BUILDER.PackageError,
                        "serialized release manifest exceeds",
                    ),
                ):
                    operation(staging)
                self.assertEqual(list(staging.iterdir()), [])

    def test_release_manifest_serialization_errors_are_package_errors(self) -> None:
        with (
            mock.patch.object(
                BUILDER.json.JSONEncoder,
                "iterencode",
                return_value=iter(["bad\ud800value"]),
            ),
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "failed to serialize release manifest",
            ),
        ):
            BUILDER._release_manifest_payload({"version": 1})

    def test_release_manifest_rejects_single_token_before_encoder(self) -> None:
        manifest = {
            "version": 1,
            "padding": "\N{PILE OF POO}" * (
                BUILDER.MAX_RELEASE_MANIFEST_BYTES // 12 + 1
            ),
        }

        with (
            mock.patch.object(
                BUILDER.json.JSONEncoder,
                "iterencode",
                side_effect=AssertionError("encoder must not run"),
            ),
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "serialized release manifest exceeds",
            ),
        ):
            BUILDER._release_manifest_payload(manifest)

    def test_release_manifest_rejects_large_array_without_materializing_payload(
        self,
    ) -> None:
        manifest = {
            "version": 1,
            "padding": [0] * (BUILDER.MAX_RELEASE_MANIFEST_BYTES // 7 + 1),
        }

        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "serialized release manifest exceeds",
        ):
            BUILDER._release_manifest_payload(manifest)

    def test_release_manifest_reports_deep_nesting_as_package_error(self) -> None:
        nested: object = None
        for _index in range(sys.getrecursionlimit() + 10):
            nested = [nested]

        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "failed to serialize release manifest",
        ):
            BUILDER._release_manifest_payload({"version": 1, "nested": nested})

    def test_manifest_parser_reports_deep_json_as_package_error(self) -> None:
        depth = sys.getrecursionlimit() + 10
        payload = (
            b'{"version":1,"nested":'
            + b"[" * depth
            + b"0"
            + b"]" * depth
            + b"}"
        )

        with self.assertRaisesRegex(
            BUILDER.PackageError,
            "invalid JSON|failed to serialize release manifest",
        ):
            manifest = BUILDER._parse_manifest_bytes(
                payload,
                Path("manifest.json"),
            )
            BUILDER._release_manifest_payload(manifest)

    def test_manifest_parser_reports_oversized_integer_as_package_error(self) -> None:
        payload = (
            b'{"version":'
            + b"9" * (BUILDER.MAX_JSON_INTEGER_DIGITS + 1)
            + b"}"
        )

        with self.assertRaisesRegex(BUILDER.PackageError, "invalid JSON"):
            BUILDER._parse_manifest_bytes(payload, Path("manifest.json"))

    def test_manifest_parser_rejects_nonstandard_json_constants(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant.decode("ascii")):
                payload = b'{"version":1,"unknown":' + constant + b"}"

                with self.assertRaisesRegex(
                    BUILDER.PackageError,
                    "invalid JSON: non-standard JSON constant",
                ):
                    BUILDER._parse_manifest_bytes(
                        payload,
                        Path("manifest.json"),
                    )

    def test_release_manifest_rejects_non_finite_programmatic_values(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaisesRegex(
                BUILDER.PackageError,
                "failed to serialize release manifest",
            ):
                BUILDER._release_manifest_payload(
                    {"version": 1, "unknown": value}
                )

    def test_release_manifest_rejects_escaped_surrogate_outside_paths(self) -> None:
        manifest = json.loads(
            rb'{"version":1,"unknown":{"nested":"\ud800"}}'.decode("utf-8")
        )

        with self.assertRaisesRegex(BUILDER.PackageError, "not valid UTF-8"):
            BUILDER._release_manifest_payload(manifest)

    def test_strict_snapshot_uses_git_inventory_without_live_tree_walk(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo()

        with mock.patch.object(
            Path,
            "rglob",
            side_effect=AssertionError("unexpected live tree walk"),
        ):
            snapshot = BUILDER._prepare_strict_release_snapshot(
                repo,
                manifest,
                head,
            )

        self.assertEqual(
            [entry.path for entry in snapshot.files],
            [Path("payload/SKILL.md")],
        )

    def test_strict_inventory_uses_one_prefix_probe_per_entry(self) -> None:
        boundary_paths = [Path("a/b")]
        boundary_index = BUILDER._strict_path_prefix_index(boundary_paths)
        for candidate, expected in (
            (Path("a"), True),
            (Path("a/b"), True),
            (Path("a/b/child"), True),
            (Path("a/bc"), False),
            (Path("other"), False),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    BUILDER._strict_inventory_path_is_selected(
                        candidate,
                        boundary_index,
                    ),
                    expected,
                )

        class CountingChildren(dict[str, BUILDER._StrictPathIndexNode]):
            probes = 0

            def get(
                self,
                key: str,
                default: None = None,
            ) -> BUILDER._StrictPathIndexNode | None:
                type(self).probes += 1
                return super().get(key, default)

        deep_path = Path(*(f"part-{index}" for index in range(512)))
        deep_index = BUILDER._strict_path_prefix_index([deep_path])

        def instrument(node: BUILDER._StrictPathIndexNode) -> None:
            node.children = CountingChildren(node.children)
            for child in node.children.values():
                instrument(child)

        instrument(deep_index)
        self.assertTrue(
            BUILDER._strict_inventory_path_is_selected(
                deep_path,
                deep_index,
            )
        )
        self.assertEqual(CountingChildren.probes, len(deep_path.parts))

        deep_descendant = deep_path / "leaf"
        deep_directory_index = BUILDER._strict_path_prefix_index([deep_path])
        CountingChildren.probes = 0
        instrument(deep_directory_index)
        self.assertEqual(
            BUILDER._strict_inventory_selected_ancestors(
                deep_descendant,
                deep_directory_index,
                proper=True,
            ),
            (deep_path,),
        )
        self.assertEqual(
            CountingChildren.probes,
            len(deep_descendant.parts),
        )

        self.assertEqual(
            BUILDER._inventory_pathspec_roots(
                [Path("a/b/c"), Path("a/d"), Path("z/file")]
            ),
            [Path("a"), Path("z")],
        )
        with mock.patch.object(BUILDER, "GIT_PATHSPEC_ARG_BUDGET_BYTES", 3):
            self.assertEqual(
                BUILDER._inventory_pathspec_roots(
                    [Path("a/file"), Path("b/file")]
                ),
                [],
            )

        selected_paths = [Path(f"payload/file-{index}") for index in range(128)]
        index_entries: dict[Path, set[object]] = {}
        tree_entries: dict[Path, object] = {}
        for index, path in enumerate(selected_paths, start=1):
            object_id = f"{index:040x}".encode("ascii")
            index_entries[path] = {
                BUILDER._IndexEntry(b"100644", object_id, b"0")
            }
            tree_entries[path] = BUILDER._TreeEntry(
                b"100644",
                b"blob",
                object_id,
            )
        unrelated = Path("unrelated/file")
        index_entries[unrelated] = {
            BUILDER._IndexEntry(b"100644", b"f" * 40, b"0")
        }
        tree_entries[unrelated] = BUILDER._TreeEntry(
            b"100644",
            b"blob",
            b"f" * 40,
        )

        with (
            mock.patch.object(
                BUILDER,
                "_bounded_git_index_inventory",
                return_value=mock.sentinel.index_inventory,
            ) as index_inventory,
            mock.patch.object(
                BUILDER,
                "_bounded_git_tree_inventory",
                return_value=mock.sentinel.tree_inventory,
            ) as tree_inventory,
            mock.patch.object(
                BUILDER,
                "_parse_index_inventory",
                return_value=index_entries,
            ),
            mock.patch.object(
                BUILDER,
                "_parse_tree_inventory",
                return_value=tree_entries,
            ),
            mock.patch.object(
                BUILDER,
                "_strict_inventory_path_is_selected",
                wraps=BUILDER._strict_inventory_path_is_selected,
            ) as probe,
        ):
            selected_index, selected_tree = BUILDER._validated_strict_entries(
                Path("."),
                "a" * 40,
                selected_paths,
            )

        self.assertEqual(set(selected_index), set(selected_paths))
        self.assertEqual(set(selected_tree), set(selected_paths))
        self.assertEqual(probe.call_count, 2 * len(index_entries))
        index_inventory.assert_called_once_with(Path("."), [Path("payload")])
        tree_inventory.assert_called_once_with(
            Path("."),
            "a" * 40,
            [Path("payload")],
        )

    def test_strict_snapshot_indexes_directory_descendants_once(self) -> None:
        class SingleScanTree(dict[Path, object]):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                self.iterations = 0

            def __iter__(self):  # type: ignore[no-untyped-def]
                self.iterations += 1
                if self.iterations > 1:
                    raise AssertionError("tree inventory was rescanned")
                return super().__iter__()

            def items(self):  # type: ignore[no-untyped-def]
                raise AssertionError("tree inventory items were rescanned")

        paths = (
            Path("outer/root.txt"),
            Path("outer/nested/child.txt"),
            Path("outer/__pycache__/ignored.pyc"),
            Path("exact.txt"),
        )
        tree_entries = SingleScanTree()
        index_entries: dict[Path, set[object]] = {}
        for index, path in enumerate(paths, start=1):
            object_id = f"{index:040x}".encode("ascii")
            tree_entries[path] = BUILDER._TreeEntry(
                b"100644",
                b"blob",
                object_id,
            )
            index_entries[path] = {
                BUILDER._IndexEntry(b"100644", object_id, b"0")
            }

        directories, files = BUILDER._strict_snapshot_entries(
            [
                Path("outer"),
                Path("outer/nested"),
                Path("exact.txt"),
                Path("outer"),
            ],
            index_entries,
            tree_entries,
        )

        self.assertEqual(
            directories,
            {Path("outer"), Path("outer/nested")},
        )
        self.assertEqual(
            set(files),
            {
                Path("outer/root.txt"),
                Path("outer/nested/child.txt"),
                Path("exact.txt"),
            },
        )
        self.assertEqual(tree_entries.iterations, 1)

    def test_strict_snapshot_fails_when_git_inventory_hits_hard_limit(self) -> None:
        repo, manifest, _source, head = self.init_tracked_repo()

        with (
            mock.patch.object(BUILDER, "GIT_INVENTORY_LIMIT_BYTES", 32),
            self.assertRaisesRegex(
                BUILDER.PackageError,
                "manifest-source inventory exceeds",
            ),
        ):
            BUILDER._prepare_strict_release_snapshot(repo, manifest, head)

    def test_manifest_paths_reject_json_nul_and_lone_surrogate(self) -> None:
        payloads = {
            "link-source-nul": rb'{"version":1,"links":[{"source":"payload\u0000file","target":"skills/example","kind":"file"}]}',
            "link-source-surrogate": rb'{"version":1,"links":[{"source":"payload\ud800file","target":"skills/example","kind":"file"}]}',
            "link-target-nul": rb'{"version":1,"links":[{"source":"payload/file","target":"skills\u0000/example","kind":"file"}]}',
            "link-target-surrogate": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/\ud800","kind":"file"}]}',
            "reference-nul": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"reference_only":["docs\u0000/readme.md"]}',
            "reference-surrogate": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"reference_only":["docs/\ud800.md"]}',
            "removed-source-nul": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"removed_links":[{"id":"old","source":"old\u0000/source","target":"skills/old","kind":"skill"}]}',
            "removed-source-surrogate": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"removed_links":[{"id":"old","source":"old/\ud800","target":"skills/old","kind":"skill"}]}',
            "removed-target-nul": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"removed_links":[{"id":"old","source":"old/source","target":"skills\u0000/old","kind":"skill"}]}',
            "removed-target-surrogate": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"removed_links":[{"id":"old","source":"old/source","target":"skills/\ud800","kind":"skill"}]}',
            "replacement-nul": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"removed_links":[{"id":"old","source":"old/source","target":"skills/old","kind":"skill","replacement_target":"skills\u0000/new"}]}',
            "replacement-surrogate": rb'{"version":1,"links":[{"source":"payload/file","target":"skills/example","kind":"file"}],"removed_links":[{"id":"old","source":"old/source","target":"skills/old","kind":"skill","replacement_target":"skills/\ud800"}]}',
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                manifest = BUILDER._parse_manifest_bytes(payload, Path("manifest.json"))
                with self.assertRaisesRegex(
                    BUILDER.PackageError,
                    "embedded NUL|valid UTF-8",
                ):
                    BUILDER._manifest_sources(manifest)

    def test_validated_repo_path_translates_embedded_nul_value_error(self) -> None:
        repo = self.root / "repo-nul"
        repo.mkdir()

        with self.assertRaisesRegex(BUILDER.PackageError, "embedded NUL"):
            BUILDER._validated_repo_path(repo, Path("bad\0path"), "manifest source")


if __name__ == "__main__":
    unittest.main()
