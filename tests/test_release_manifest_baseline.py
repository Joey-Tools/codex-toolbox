from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
from io import StringIO
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_sync_manifest_changes.py"
SPEC = importlib.util.spec_from_file_location(
    "validate_sync_manifest_changes_release_baseline",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
ORIGINAL_READ_VERIFIED_RELEASE_MANIFEST = MODULE._read_verified_release_manifest

RUNTIME_SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_release_manifest_baseline",
    RUNTIME_SCRIPT_PATH,
)
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
assert RUNTIME_SPEC is not None
assert RUNTIME_SPEC.loader is not None
sys.modules[RUNTIME_SPEC.name] = RUNTIME
RUNTIME_SPEC.loader.exec_module(RUNTIME)

MANIFEST = Path(
    "personal_codex/private-sync-manifest.json"
    if (REPO_ROOT / "personal_codex/private-sync-manifest.json").is_file()
    else "personal_codex/public-sync-manifest.json"
)


def manifest(
    *skills: str,
    removed_links: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "links": [
            {
                "source": f"personal_codex/skills/{skill}",
                "target": f"skills/{skill}",
                "kind": "skill",
            }
            for skill in skills
        ],
        "reference_only": [],
    }
    if removed_links is not None:
        payload["removed_links"] = removed_links
    return payload


def removed(skill: str, removed_id: str) -> dict[str, object]:
    return {
        "id": removed_id,
        "source": f"personal_codex/skills/{skill}",
        "target": f"skills/{skill}",
        "kind": "skill",
        "legacy": False,
    }


def complete_release(
    sha: str,
    *,
    tag: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
    asset_state: str = "uploaded",
    published_at: str = "2026-07-15T00:00:00Z",
    archive_id: int | None = None,
    checksum_id: int | None = None,
    archive_size: int = 1,
    checksum_size: int = 1,
) -> dict[str, object]:
    asset_id_base = int(sha[:12], 16) * 2 + 1
    return {
        "tag_name": tag or f"personal-codex-20260715-000000-{sha[:7]}",
        "target_commitish": sha,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published_at,
        "assets": [
            {
                "id": archive_id if archive_id is not None else asset_id_base,
                "name": f"personal-codex-{sha}.tar.gz",
                "size": archive_size,
                "state": asset_state,
            },
            {
                "id": checksum_id if checksum_id is not None else asset_id_base + 1,
                "name": f"personal-codex-{sha}.sha256",
                "size": checksum_size,
                "state": asset_state,
            },
        ],
    }


class VerifiedReleaseManifestRuntimeTests(unittest.TestCase):
    SHA = "a" * 40

    @classmethod
    def manifest_member_path(cls, *, root: str | None = None) -> str:
        release_root = root or f"personal-codex-{cls.SHA}"
        return f"{release_root}/personal_codex/sync-manifest.json"

    @staticmethod
    def archive_payload(entries: list[tuple[str, bytes]]) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, payload in entries:
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        return output.getvalue()

    def assets_for_payloads(
        self,
        archive_payload: bytes,
        checksum_payload: bytes,
    ) -> object:
        return RUNTIME.ReleaseAssets(
            tag_name=f"personal-codex-20260715-000000-{self.SHA[:7]}",
            sha=self.SHA,
            archive_name=f"personal-codex-{self.SHA}.tar.gz",
            archive_id=101,
            archive_size=len(archive_payload),
            checksum_name=f"personal-codex-{self.SHA}.sha256",
            checksum_id=102,
            checksum_size=len(checksum_payload),
        )

    def checksum_payload(self, archive_payload: bytes) -> bytes:
        digest = hashlib.sha256(archive_payload).hexdigest()
        return (
            f"{digest}  personal-codex-{self.SHA}.tar.gz\n".encode("ascii")
        )

    def read_with_fake_downloads(
        self,
        archive_payload: bytes,
        checksum_payload: bytes,
        *,
        maximum_expanded_bytes: int | None = None,
    ) -> tuple[object, list[tuple[str, int, int, int]]]:
        assets = self.assets_for_payloads(archive_payload, checksum_payload)
        payloads = {
            assets.archive_name: archive_payload,
            assets.checksum_name: checksum_payload,
        }
        calls: list[tuple[str, int, int, int]] = []

        def fake_download(
            _repo: str,
            asset_name: str,
            asset_id: int,
            expected_size: int,
            maximum_bytes: int,
            destination: Path,
            *,
            bound_destination_fd: int,
        ) -> None:
            calls.append((asset_name, asset_id, expected_size, maximum_bytes))
            payload = payloads[asset_name]
            self.assertEqual(len(payload), expected_size)
            opened_metadata = os.fstat(bound_destination_fd)
            path_metadata = destination.stat()
            self.assertEqual(
                (opened_metadata.st_dev, opened_metadata.st_ino),
                (path_metadata.st_dev, path_metadata.st_ino),
            )
            (destination / asset_name).write_bytes(payload)

        with mock.patch.object(
            RUNTIME,
            "_download_release_asset",
            side_effect=fake_download,
        ):
            kwargs = (
                {}
                if maximum_expanded_bytes is None
                else {"maximum_expanded_bytes": maximum_expanded_bytes}
            )
            result = RUNTIME.read_verified_release_manifest(
                "owner/repo",
                assets,
                **kwargs,
            )
        return result, calls

    def test_reads_exact_manifest_with_exact_asset_ids_and_sizes(self) -> None:
        expected_manifest = {
            "version": 1,
            "links": [],
            "unknown": {"preserved": True},
        }
        manifest_payload = json.dumps(expected_manifest).encode("utf-8")
        archive_payload = self.archive_payload(
            [(self.manifest_member_path(), manifest_payload)]
        )
        checksum_payload = self.checksum_payload(archive_payload)

        result, calls = self.read_with_fake_downloads(
            archive_payload,
            checksum_payload,
        )

        self.assertEqual(result.manifest, expected_manifest)
        self.assertGreater(result.expanded_bytes, len(manifest_payload) * 2)
        self.assertEqual(
            calls,
            [
                (
                    f"personal-codex-{self.SHA}.tar.gz",
                    101,
                    len(archive_payload),
                    RUNTIME.MAX_ARCHIVE_COMPRESSED_BYTES,
                ),
                (
                    f"personal-codex-{self.SHA}.sha256",
                    102,
                    len(checksum_payload),
                    RUNTIME.MAX_ARCHIVE_CHECKSUM_BYTES,
                ),
            ],
        )

    def test_rejects_bad_checksum(self) -> None:
        manifest_payload = b'{"version":1,"links":[]}'
        archive_payload = self.archive_payload(
            [(self.manifest_member_path(), manifest_payload)]
        )
        checksum_payload = (
            f"{'0' * 64}  personal-codex-{self.SHA}.tar.gz\n".encode("ascii")
        )

        with self.assertRaisesRegex(RUNTIME.SyncError, "checksum mismatch"):
            self.read_with_fake_downloads(archive_payload, checksum_payload)

    def test_rejects_wrong_duplicate_and_oversized_manifest_entries(self) -> None:
        valid_payload = b'{"version":1,"links":[]}'
        cases = (
            (
                "wrong-root",
                [(self.manifest_member_path(root="wrong-root"), valid_payload)],
                "exactly one release manifest",
            ),
            (
                "duplicate",
                [
                    (self.manifest_member_path(), valid_payload),
                    (self.manifest_member_path(), valid_payload),
                ],
                "duplicate archive member path",
            ),
            (
                "oversized",
                [
                    (
                        self.manifest_member_path(),
                        b"x" * (RUNTIME.MAX_RELEASE_MANIFEST_BYTES + 1),
                    )
                ],
                "release manifest exceeds byte limit",
            ),
        )
        for name, entries, error_pattern in cases:
            with self.subTest(name=name):
                archive_payload = self.archive_payload(entries)
                checksum_payload = self.checksum_payload(archive_payload)
                with self.assertRaisesRegex(RUNTIME.SyncError, error_pattern):
                    self.read_with_fake_downloads(
                        archive_payload,
                        checksum_payload,
                    )

    def test_rejects_truncated_archive_after_checksum_verification(self) -> None:
        archive_payload = self.archive_payload(
            [(self.manifest_member_path(), b'{"version":1,"links":[]}')]
        )
        truncated_payload = archive_payload[: len(archive_payload) // 2]
        checksum_payload = self.checksum_payload(truncated_payload)

        with self.assertRaisesRegex(
            RUNTIME.SyncError,
            "failed to inspect archive snapshot|ended before all planned members",
        ):
            self.read_with_fake_downloads(truncated_payload, checksum_payload)

    def test_expanded_budget_is_enforced_across_both_archive_passes(self) -> None:
        manifest_payload = b'{"version":1,"links":[]}'
        archive_payload = self.archive_payload(
            [(self.manifest_member_path(), manifest_payload)]
        )
        checksum_payload = self.checksum_payload(archive_payload)
        initial, _calls = self.read_with_fake_downloads(
            archive_payload,
            checksum_payload,
        )

        exact, _calls = self.read_with_fake_downloads(
            archive_payload,
            checksum_payload,
            maximum_expanded_bytes=initial.expanded_bytes,
        )
        self.assertEqual(exact.expanded_bytes, initial.expanded_bytes)
        with self.assertRaisesRegex(
            RUNTIME.SyncError,
            "expanded byte limit",
        ):
            self.read_with_fake_downloads(
                archive_payload,
                checksum_payload,
                maximum_expanded_bytes=initial.expanded_bytes - 1,
            )


class ReleaseManifestBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo = Path(self.temporary_directory.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        archive_reader_patch = mock.patch.object(
            MODULE,
            "_read_verified_release_manifest",
            side_effect=self.read_commit_manifest_as_archive,
        )
        self.archive_manifest_reader = archive_reader_patch.start()
        self.addCleanup(archive_reader_patch.stop)

    def read_commit_manifest_as_archive(
        self,
        _repository: str,
        identity: object,
        _maximum_expanded_bytes: int,
    ) -> tuple[dict[str, object], int]:
        sha = getattr(identity, "sha")
        payload = MODULE._manifest_at_ref(self.repo, sha, MANIFEST)
        if payload is None:
            raise AssertionError(f"missing test manifest at {sha}")
        return payload, 0

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def write_manifest(self, payload: dict[str, object]) -> None:
        path = self.repo / MANIFEST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        raw_links = payload.get("links", [])
        assert isinstance(raw_links, list)
        for raw_link in raw_links:
            assert isinstance(raw_link, dict)
            source = raw_link["source"]
            kind = raw_link["kind"]
            assert isinstance(source, str)
            source_path = self.repo / source
            if kind == "file":
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text("source\n", encoding="utf-8")
                continue
            source_path.mkdir(parents=True, exist_ok=True)
            if kind == "skill":
                (source_path / "SKILL.md").write_text(
                    "# Skill\n",
                    encoding="utf-8",
                )

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--no-gpg-sign",
            "-qm",
            message,
        )
        return self.git("rev-parse", "HEAD")

    def release_patch(self, release: dict[str, object]):
        return mock.patch.object(
            MODULE,
            "_iter_github_releases",
            side_effect=lambda *_args: iter([release]),
        )

    def test_release_repo_and_base_ref_are_mutually_exclusive(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            MODULE.build_parser().parse_args(
                ["--base-ref", "HEAD", "--release-repo", "owner/repo"]
            )

    def test_repair_incomplete_head_release_requires_release_repo(self) -> None:
        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "requires --release-repo",
        ):
            MODULE.main(["--repair-incomplete-head-release"])

    def test_github_response_body_limit_accepts_boundary_and_rejects_overflow(
        self,
    ) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        with (
            mock.patch.object(MODULE, "MAX_GITHUB_API_RESPONSE_BYTES", 2),
            mock.patch.object(MODULE, "urlopen", return_value=response),
        ):
            self.assertEqual(MODULE._request_json("https://example.test", "token"), [])
        response.read.assert_called_once_with(3)

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[] "
        with (
            mock.patch.object(MODULE, "MAX_GITHUB_API_RESPONSE_BYTES", 2),
            mock.patch.object(MODULE, "urlopen", return_value=response),
            self.assertRaisesRegex(MODULE.ValidationError, "response exceeds byte limit"),
        ):
            MODULE._request_json("https://example.test", "token")
        response.read.assert_called_once_with(3)

    def test_github_response_json_depth_error_is_normalized(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        with (
            mock.patch.object(MODULE, "urlopen", return_value=response),
            mock.patch.object(
                MODULE.json,
                "loads",
                side_effect=RecursionError("maximum nesting exceeded"),
            ),
            self.assertRaisesRegex(MODULE.ValidationError, "returned invalid JSON"),
        ):
            MODULE._request_json("https://example.test", "token")

    def test_github_release_limit_accepts_boundary_and_rejects_overflow(
        self,
    ) -> None:
        release = complete_release("a" * 40)
        with (
            mock.patch.object(MODULE, "MAX_GITHUB_RELEASES", 2),
            mock.patch.object(
                MODULE,
                "_request_json",
                return_value=[release, release],
            ),
        ):
            self.assertEqual(
                len(list(MODULE._iter_github_releases("owner/repo", "token"))),
                2,
            )

        with (
            mock.patch.object(MODULE, "MAX_GITHUB_RELEASES", 2),
            mock.patch.object(
                MODULE,
                "_request_json",
                return_value=[release, release, release],
            ),
            self.assertRaisesRegex(MODULE.ValidationError, "exceeds release limit"),
        ):
            list(MODULE._iter_github_releases("owner/repo", "token"))

    def test_github_pagination_limit_accepts_boundary_and_rejects_overflow(
        self,
    ) -> None:
        release = complete_release("a" * 40)
        with (
            mock.patch.object(MODULE, "GITHUB_RELEASES_PAGE_SIZE", 2),
            mock.patch.object(MODULE, "MAX_GITHUB_RELEASE_PAGES", 2),
            mock.patch.object(MODULE, "MAX_GITHUB_RELEASES", 3),
            mock.patch.object(
                MODULE,
                "_request_json",
                side_effect=[[release, release], [release]],
            ) as request_json,
        ):
            self.assertEqual(
                len(list(MODULE._iter_github_releases("owner/repo", "token"))),
                3,
            )
        self.assertEqual(request_json.call_count, 2)

        with (
            mock.patch.object(MODULE, "GITHUB_RELEASES_PAGE_SIZE", 1),
            mock.patch.object(MODULE, "MAX_GITHUB_RELEASE_PAGES", 2),
            mock.patch.object(MODULE, "MAX_GITHUB_RELEASES", 3),
            mock.patch.object(
                MODULE,
                "_request_json",
                side_effect=[[release], [release]],
            ) as request_json,
            self.assertRaisesRegex(MODULE.ValidationError, "exceeds pagination limit"),
        ):
            list(MODULE._iter_github_releases("owner/repo", "token"))
        self.assertEqual(request_json.call_count, 2)

    def test_complete_releases_paginate_and_skip_incomplete_entries(
        self,
    ) -> None:
        sha = "a" * 40
        draft = complete_release(sha, draft=True)
        prerelease = complete_release(sha, prerelease=True)
        unrelated = {
            "tag_name": "v1.0.0",
            "draft": False,
            "prerelease": False,
            "assets": [],
        }
        first_page = [draft, prerelease, unrelated] + [draft] * 97
        expected = complete_release(
            sha,
            published_at="2026-07-15T01:00:00Z",
        )
        older = complete_release(
            "b" * 40,
            published_at="2026-07-14T23:00:00Z",
        )

        with mock.patch.object(
            MODULE,
            "_request_json",
            side_effect=[first_page, [older, expected]],
        ) as request_json:
            identities = MODULE._complete_release_identities(
                "owner/repo",
                "token",
            )
            self.assertEqual(
                [(identity.tag_name, identity.sha) for identity in identities],
                [
                    (older["tag_name"], older["target_commitish"]),
                    (expected["tag_name"], sha),
                ],
            )

        self.assertEqual(request_json.call_count, 2)
        self.assertIn("page=1", request_json.call_args_list[0].args[0])
        self.assertIn("page=2", request_json.call_args_list[1].args[0])

    def test_published_personal_release_requires_complete_uploaded_pair(
        self,
    ) -> None:
        sha = "a" * 40
        cases = (
            ("missing-assets", {"assets": []}, "missing.*tarball"),
            (
                "pending-assets",
                {"asset_state": "new"},
                "not uploaded",
            ),
            (
                "mismatched-checksum",
                {"checksum_sha": "c" * 40},
                "missing.*matching checksum",
            ),
        )
        for name, mutation, error_pattern in cases:
            with self.subTest(name=name):
                release = complete_release(
                    sha,
                    asset_state=str(mutation.get("asset_state", "uploaded")),
                )
                if "assets" in mutation:
                    release["assets"] = mutation["assets"]
                checksum_sha = mutation.get("checksum_sha")
                if checksum_sha is not None:
                    release["assets"][1]["name"] = (
                        f"personal-codex-{checksum_sha}.sha256"
                    )
                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    error_pattern,
                ):
                    MODULE._complete_release_identity(release)

    def test_repair_skips_only_nonexact_matching_assets_for_exact_sha(
        self,
    ) -> None:
        current_sha = "a" * 40
        historical_sha = "b" * 40
        historical_release = complete_release(historical_sha)
        repairable_releases = [complete_release(current_sha) for _ in range(4)]
        for release_id, release in enumerate(repairable_releases, start=123):
            release["id"] = release_id
        repairable_releases[0]["assets"] = []
        repairable_releases[1]["assets"][1]["state"] = "starter"
        repairable_releases[2]["assets"].append(
            {
                "id": 999,
                "name": f"personal-codex-{'c' * 40}.sha256",
                "size": 1,
                "state": "starter",
            }
        )
        repairable_releases[3]["assets"].append(
            {
                "id": 999,
                "name": f"personal-codex-{'c' * 40}.tar.gz",
                "size": 1,
                "state": "uploaded",
            }
        )

        for current_release in repairable_releases:
            with (
                self.subTest(assets=current_release["assets"]),
                mock.patch.object(
                    MODULE,
                    "_iter_github_releases",
                    side_effect=lambda *_args, release=current_release: iter(
                        [release, historical_release]
                    ),
                ),
            ):
                identities = MODULE._complete_release_identities(
                    "owner/repo",
                    "token",
                    repair_incomplete_release_sha=current_sha,
                )

            self.assertEqual(
                [(identity.tag_name, identity.sha) for identity in identities],
                [(historical_release["tag_name"], historical_sha)],
            )

        complete_current = complete_release(current_sha)
        with mock.patch.object(
            MODULE,
            "_iter_github_releases",
            side_effect=lambda *_args: iter(
                [complete_current, historical_release]
            ),
        ):
            identities = MODULE._complete_release_identities(
                "owner/repo",
                "token",
                repair_incomplete_release_sha=current_sha,
            )
        self.assertEqual(
            [identity.sha for identity in identities],
            [current_sha, historical_sha],
        )

        with (
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(
                    [repairable_releases[0], historical_release]
                ),
            ),
            self.assertRaises(MODULE.ValidationError),
        ):
            MODULE._complete_release_identities(
                "owner/repo",
                "token",
                repair_incomplete_release_sha="c" * 40,
            )

    def test_repair_requires_valid_unique_matching_asset_ids(self) -> None:
        current_sha = "a" * 40
        historical_release = complete_release("b" * 40)
        cases = (
            ("missing", None),
            ("boolean", True),
            ("zero", 0),
            ("negative", -1),
            ("duplicate", complete_release(current_sha)["assets"][0]["id"]),
        )
        for name, asset_id in cases:
            current_release = complete_release(current_sha)
            current_release["id"] = 123
            extra_asset = {
                "name": f"personal-codex-{'c' * 40}.sha256",
                "size": 1,
                "state": "starter",
            }
            if asset_id is not None:
                extra_asset["id"] = asset_id
            current_release["assets"].append(extra_asset)
            with (
                self.subTest(name=name),
                mock.patch.object(
                    MODULE,
                    "_iter_github_releases",
                    side_effect=lambda *_args, release=current_release: iter(
                        [release, historical_release]
                    ),
                ),
                self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "valid GitHub asset id|reuses GitHub asset id",
                ),
            ):
                MODULE._complete_release_identities(
                    "owner/repo",
                    "token",
                    repair_incomplete_release_sha=current_sha,
                )

    def test_repair_preserves_immutable_metadata_and_history_validation(
        self,
    ) -> None:
        current_sha = "a" * 40
        historical_release = complete_release("b" * 40)
        invalid_releases = []

        invalid_tag = complete_release(current_sha)
        invalid_tag["id"] = 123
        invalid_tag["tag_name"] = "personal-codex-invalid"
        invalid_tag["assets"] = []
        invalid_releases.append((invalid_tag, "invalid tag name"))

        mismatched_tag = complete_release(current_sha)
        mismatched_tag["id"] = 123
        mismatched_tag["tag_name"] = (
            f"personal-codex-20260715-000000-{'c' * 7}"
        )
        mismatched_tag["assets"] = []
        invalid_releases.append((mismatched_tag, "does not match tag suffix"))

        invalid_timestamp = complete_release(current_sha)
        invalid_timestamp["id"] = 123
        invalid_timestamp["published_at"] = "invalid"
        invalid_timestamp["assets"] = []
        invalid_releases.append((invalid_timestamp, "published_at"))

        invalid_assets = complete_release(current_sha)
        invalid_assets["id"] = 123
        invalid_assets["assets"] = None
        invalid_releases.append((invalid_assets, "no asset array"))

        invalid_release_id = complete_release(current_sha)
        invalid_release_id["id"] = True
        invalid_release_id["assets"] = []
        invalid_releases.append((invalid_release_id, "GitHub release id"))

        for current_release, error_pattern in invalid_releases:
            with (
                self.subTest(error_pattern=error_pattern),
                mock.patch.object(
                    MODULE,
                    "_iter_github_releases",
                    side_effect=lambda *_args, release=current_release: iter(
                        [release, historical_release]
                    ),
                ),
                self.assertRaisesRegex(MODULE.ValidationError, error_pattern),
            ):
                MODULE._complete_release_identities(
                    "owner/repo",
                    "token",
                    repair_incomplete_release_sha=current_sha,
                )

        repairable_current = complete_release(current_sha)
        repairable_current["id"] = 123
        repairable_current["assets"] = []
        historical_release["assets"] = []
        with (
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(
                    [repairable_current, historical_release]
                ),
            ),
            self.assertRaisesRegex(MODULE.ValidationError, "missing.*tarball"),
        ):
            MODULE._complete_release_identities(
                "owner/repo",
                "token",
                repair_incomplete_release_sha=current_sha,
            )

    def test_repair_rejects_multiple_matching_head_releases(self) -> None:
        current_sha = "a" * 40
        historical_release = complete_release("b" * 40)
        incomplete_releases = [complete_release(current_sha) for _ in range(2)]
        for release_id, release in enumerate(incomplete_releases, start=123):
            release["id"] = release_id
            release["assets"] = []

        with (
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(
                    [*incomplete_releases, historical_release]
                ),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "multiple repairable incomplete published releases",
            ),
        ):
            MODULE._complete_release_identities(
                "owner/repo",
                "token",
                repair_incomplete_release_sha=current_sha,
            )

    def test_release_history_repair_resolves_head_internally(self) -> None:
        head_sha = "a" * 40
        with (
            mock.patch.object(MODULE, "_resolve_commit", return_value=head_sha) as resolve,
            mock.patch.object(MODULE, "_github_token", return_value="token"),
            mock.patch.object(
                MODULE,
                "_complete_release_identities",
                side_effect=MODULE.ValidationError("stop after identity lookup"),
            ) as identities,
            self.assertRaisesRegex(MODULE.ValidationError, "stop after identity lookup"),
        ):
            MODULE._release_history_baseline(
                self.repo,
                "owner/repo",
                MANIFEST,
                repair_incomplete_head_release=True,
            )
        resolve.assert_called_once_with(self.repo, "HEAD")
        identities.assert_called_once_with(
            "owner/repo",
            "token",
            repair_incomplete_release_sha=head_sha,
        )

    def test_published_personal_release_rejects_mixed_asset_states(self) -> None:
        sha = "a" * 40
        other_sha = "b" * 40
        cases = (
            ("duplicate-archive", f"personal-codex-{sha}.tar.gz"),
            ("other-archive", f"personal-codex-{other_sha}.tar.gz"),
            ("duplicate-checksum", f"personal-codex-{sha}.sha256"),
            ("other-checksum", f"personal-codex-{other_sha}.sha256"),
        )
        for name, asset_name in cases:
            with self.subTest(name=name):
                release = complete_release(sha)
                release["assets"].append(
                    {
                        "id": 999,
                        "name": asset_name,
                        "size": 1,
                        "state": "new",
                    }
                )
                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "not uploaded",
                ):
                    MODULE._complete_release_identity(release)

    def test_complete_release_identity_uses_asset_sha_and_rejects_conflicts(
        self,
    ) -> None:
        sha = "a" * 40
        release = complete_release(sha)
        release["target_commitish"] = "main"
        identity = MODULE._complete_release_identity(release)
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.published_at, release["published_at"])
        self.assertEqual(identity.tag_name, release["tag_name"])
        self.assertEqual(identity.sha, sha)

        mismatched_target = complete_release(sha)
        mismatched_target["target_commitish"] = "b" * 40
        with self.assertRaisesRegex(MODULE.ValidationError, "target commit"):
            MODULE._complete_release_identity(mismatched_target)

        mismatched_tag = complete_release(
            sha,
            tag=f"personal-codex-20260715-000000-{'b' * 7}",
        )
        with self.assertRaisesRegex(MODULE.ValidationError, "tag suffix"):
            MODULE._complete_release_identity(mismatched_tag)

        duplicate_archive = complete_release(sha)
        duplicate_archive["assets"].append(
            {
                "name": f"personal-codex-{sha}.tar.gz",
                "state": "uploaded",
            }
        )
        with self.assertRaisesRegex(MODULE.ValidationError, "multiple.*tarball"):
            MODULE._complete_release_identity(duplicate_archive)

    def test_complete_release_identity_requires_exact_asset_metadata(self) -> None:
        sha = "a" * 40
        cases = (
            ("missing-id", None, 1, "asset id"),
            ("boolean-id", True, 1, "asset id"),
            ("zero-id", 0, 1, "asset id"),
            ("missing-size", 101, None, "asset size"),
            ("boolean-size", 101, False, "asset size"),
            ("negative-size", 101, -1, "asset size"),
        )
        for name, asset_id, asset_size, error_pattern in cases:
            with self.subTest(name=name):
                release = complete_release(sha)
                archive = release["assets"][0]
                if asset_id is None:
                    archive.pop("id")
                else:
                    archive["id"] = asset_id
                if asset_size is None:
                    archive.pop("size")
                else:
                    archive["size"] = asset_size
                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    error_pattern,
                ):
                    MODULE._complete_release_identity(release)

    def test_release_metadata_preflight_fails_before_archive_download(self) -> None:
        sha = "a" * 40
        release = complete_release(sha, archive_size=2)
        cases = (
            ("count", [release, release], "MAX_COMPLETE_RELEASES", 1, "count"),
            (
                "archive-total",
                [release],
                "MAX_RELEASE_ARCHIVE_TOTAL_BYTES",
                1,
                "compressed byte total",
            ),
            (
                "checksum-total",
                [complete_release(sha, checksum_size=2)],
                "MAX_RELEASE_CHECKSUM_TOTAL_BYTES",
                1,
                "checksums exceed byte total",
            ),
        )
        for name, releases, limit_name, limit, error_pattern in cases:
            with (
                self.subTest(name=name),
                mock.patch.object(
                    MODULE,
                    "_iter_github_releases",
                    side_effect=lambda *_args, values=releases: iter(values),
                ),
                mock.patch.object(MODULE, limit_name, limit),
                self.assertRaisesRegex(MODULE.ValidationError, error_pattern),
            ):
                MODULE._complete_release_identities("owner/repo", "token")
            self.archive_manifest_reader.assert_not_called()

    def test_release_baseline_accepts_verified_release_history(self) -> None:
        self.write_manifest(manifest("keep"))
        baseline_sha = self.commit("Add manifest")
        tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        self.git("tag", tag, baseline_sha)
        (self.repo / "README.md").write_text("current\n", encoding="utf-8")
        self.commit("Advance history")
        release = complete_release(baseline_sha, tag=tag)

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), self.release_patch(
            release
        ):
            base_ref, previous = MODULE._release_baseline(
                self.repo,
                "owner/repo",
                MANIFEST,
            )

        self.assertEqual(base_ref, baseline_sha)
        self.assertEqual(previous, manifest("keep"))
        self.archive_manifest_reader.assert_called_once()
        identity = self.archive_manifest_reader.call_args.args[1]
        self.assertEqual(identity.archive_id, release["assets"][0]["id"])
        self.assertEqual(identity.archive_size, release["assets"][0]["size"])
        self.assertEqual(identity.checksum_id, release["assets"][1]["id"])
        self.assertEqual(identity.checksum_size, release["assets"][1]["size"])

    def test_release_baseline_rejects_archive_manifest_mismatch(self) -> None:
        self.write_manifest(manifest("keep"))
        baseline_sha = self.commit("Add manifest")
        tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        self.git("tag", tag, baseline_sha)
        release = complete_release(baseline_sha, tag=tag)
        self.archive_manifest_reader.side_effect = None
        self.archive_manifest_reader.return_value = (manifest("different"), 1)

        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            self.release_patch(release),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "archive manifest does not match Git commit manifest",
            ),
        ):
            MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

    def test_release_archive_manifest_comparison_is_json_type_sensitive(self) -> None:
        sha = "a" * 40
        identity = MODULE._complete_release_identity(complete_release(sha))
        self.assertIsNotNone(identity)
        assert identity is not None
        cases = (
            ("bool-int", True, 1),
            ("false-zero", False, 0),
            ("int-float", 1, 1.0),
        )
        for name, commit_value, archive_value in cases:
            with self.subTest(name=name):
                self.archive_manifest_reader.reset_mock()
                self.archive_manifest_reader.side_effect = None
                self.archive_manifest_reader.return_value = (
                    {"value": archive_value},
                    0,
                )
                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "archive manifest does not match Git commit manifest",
                ):
                    MODULE._verify_release_archive_manifests(
                        "owner/repo",
                        [identity],
                        {sha: {"value": commit_value}},
                    )

    def test_release_archive_verification_caches_only_identical_asset_pairs(
        self,
    ) -> None:
        self.write_manifest(manifest("keep"))
        baseline_sha = self.commit("Add manifest")
        tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        self.git("tag", tag, baseline_sha)
        first = complete_release(
            baseline_sha,
            tag=tag,
            archive_id=101,
            checksum_id=102,
        )
        repeated = complete_release(
            baseline_sha,
            tag=tag,
            archive_id=101,
            checksum_id=102,
        )
        distinct = complete_release(
            baseline_sha,
            tag=tag,
            archive_id=201,
            checksum_id=202,
        )
        releases = [first, repeated, distinct]

        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
        ):
            MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

        self.assertEqual(self.archive_manifest_reader.call_count, 2)
        verified_pairs = {
            (
                call.args[1].archive_id,
                call.args[1].checksum_id,
            )
            for call in self.archive_manifest_reader.call_args_list
        }
        self.assertEqual(verified_pairs, {(101, 102), (201, 202)})

    def test_release_archive_expanded_total_is_enforced(self) -> None:
        self.write_manifest(manifest("keep"))
        baseline_sha = self.commit("Add manifest")
        tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        self.git("tag", tag, baseline_sha)
        releases = [
            complete_release(
                baseline_sha,
                tag=tag,
                archive_id=101,
                checksum_id=102,
            ),
            complete_release(
                baseline_sha,
                tag=tag,
                archive_id=201,
                checksum_id=202,
            ),
        ]
        self.archive_manifest_reader.side_effect = None
        self.archive_manifest_reader.return_value = (manifest("keep"), 1)

        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            mock.patch.object(MODULE, "MAX_RELEASE_EXPANDED_SCAN_TOTAL_BYTES", 1),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "exceeded its expanded byte budget",
            ),
        ):
            MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

        self.assertEqual(self.archive_manifest_reader.call_count, 2)
        self.assertEqual(
            [call.args[2] for call in self.archive_manifest_reader.call_args_list],
            [1, 0],
        )

    def test_runtime_sync_error_is_normalized(self) -> None:
        sha = "a" * 40
        identity = MODULE._complete_release_identity(complete_release(sha))
        self.assertIsNotNone(identity)
        assert identity is not None
        runtime = MODULE._sync_runtime_module()
        with (
            mock.patch.object(
                runtime,
                "read_verified_release_manifest",
                side_effect=runtime.SyncError("checksum mismatch"),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "failed to verify release archive.*checksum mismatch",
            ),
        ):
            ORIGINAL_READ_VERIFIED_RELEASE_MANIFEST(
                "owner/repo",
                identity,
                MODULE.MAX_RELEASE_EXPANDED_SCAN_TOTAL_BYTES,
            )

    def test_release_baseline_uses_newer_branch_target_release(self) -> None:
        self.write_manifest(manifest("keep", "retired"))
        older_sha = self.commit("Publish initial manifest")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        removal = removed("retired", "remove-retired")
        self.write_manifest(manifest("keep", removed_links=[removal]))
        newer_sha = self.commit("Publish removal history")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        self.write_manifest(manifest("keep"))
        self.commit("Advance history")
        newer_release = complete_release(newer_sha, tag=newer_tag)
        newer_release["target_commitish"] = "main"
        releases = [
            complete_release(older_sha, tag=older_tag),
            newer_release,
        ]

        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
        ):
            base_ref, previous = MODULE._release_baseline(
                self.repo,
                "owner/repo",
                MANIFEST,
            )

        self.assertEqual(base_ref, newer_sha)
        self.assertEqual(previous, manifest("keep", removed_links=[removal]))

    def test_release_validation_checks_skip_upgrade_capacity(self) -> None:
        self.write_manifest(manifest("keep", "old-one", "old-two"))
        older_sha = self.commit("Publish larger historical manifest")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        self.write_manifest(manifest("keep"))
        newer_sha = self.commit("Publish smaller historical manifest")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        self.write_manifest(manifest("keep", "new-one", "new-two"))
        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 4),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                f"from release {older_sha}: 5 > 4",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

    def test_release_validation_checks_skip_upgrade_target_hierarchy(self) -> None:
        historical = manifest("parent")
        historical["links"][0]["target"] = "skills"
        self.write_manifest(historical)
        older_sha = self.commit("Publish historical parent target")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        current = manifest("child")
        current["links"][0]["target"] = "skills/nested"
        self.write_manifest(current)
        newer_sha = self.commit("Publish current child target")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "hierarchy changes are not supported",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

    def test_release_validation_rejects_laundered_unrecorded_removal(self) -> None:
        self.write_manifest(manifest("keep", "retired"))
        older_sha = self.commit("Publish active retired link")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        self.write_manifest(manifest("keep"))
        newer_sha = self.commit("Publish unrecorded removal")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                f"later matching removed_links entry from release {older_sha}",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

    def test_release_validation_rejects_reused_historical_removal_id(self) -> None:
        old_removal = removed("retired", "old-remove-retired")
        self.write_manifest(
            manifest("keep", "retired", removed_links=[old_removal])
        )
        older_sha = self.commit("Republish a previously removed link")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        self.write_manifest(manifest("keep", removed_links=[old_removal]))
        newer_sha = self.commit("Remove link without a new removal id")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                f"later matching removed_links entry from release {older_sha}",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

    def test_release_validation_allows_later_legacy_removal_repair(self) -> None:
        old_removal = removed("retired", "old-remove-retired")
        self.write_manifest(
            manifest("keep", "retired", removed_links=[old_removal])
        )
        older_sha = self.commit("Republish a previously removed link")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        self.write_manifest(manifest("keep", removed_links=[old_removal]))
        newer_sha = self.commit("Publish unrecorded repeated removal")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        repair_removal = removed("retired", "repair-remove-retired")
        repair_removal["legacy"] = True
        self.write_manifest(
            manifest(
                "keep",
                removed_links=[old_removal, repair_removal],
            )
        )
        self.commit("Repair historical removal ledger")
        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            redirect_stdout(StringIO()),
        ):
            result = MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

        self.assertEqual(result, 0)

    def test_release_validation_allows_multiple_later_removal_episodes(
        self,
    ) -> None:
        self.write_manifest(manifest("keep", "retired"))
        oldest_sha = self.commit("Publish initial active link")
        oldest_tag = f"personal-codex-20260715-000000-{oldest_sha[:7]}"
        self.git("tag", oldest_tag, oldest_sha)

        first_removal = removed("retired", "first-remove-retired")
        self.write_manifest(manifest("keep", removed_links=[first_removal]))
        middle_sha = self.commit("Publish first removal")
        middle_tag = f"personal-codex-20260715-010000-{middle_sha[:7]}"
        self.git("tag", middle_tag, middle_sha)

        self.write_manifest(
            manifest("keep", "retired", removed_links=[first_removal])
        )
        newest_sha = self.commit("Republish retired link")
        newest_tag = f"personal-codex-20260715-020000-{newest_sha[:7]}"
        self.git("tag", newest_tag, newest_sha)

        second_removal = removed("retired", "second-remove-retired")
        self.write_manifest(
            manifest(
                "keep",
                removed_links=[first_removal, second_removal],
            )
        )
        self.commit("Publish second removal")
        releases = [
            complete_release(oldest_sha, tag=oldest_tag),
            complete_release(middle_sha, tag=middle_tag),
            complete_release(newest_sha, tag=newest_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            redirect_stdout(StringIO()),
        ):
            result = MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

        self.assertEqual(result, 0)

    def test_historical_removal_requires_replacement_retirement(self) -> None:
        replacement = removed("old", "replace-old")
        replacement["replacement_target"] = "skills/retired"
        historical = manifest(
            "retired",
            removed_links=[replacement],
        )
        historical["owner"] = "overlay"

        later_removal = removed("retired", "remove-retired")
        later_removal["legacy"] = True
        current = manifest(
            "keep",
            removed_links=[replacement, later_removal],
        )
        current["owner"] = "overlay"

        historical_model = MODULE._manifest_model(
            historical,
            enforce_history_constraints=False,
        )
        current_model = MODULE._manifest_model(current)
        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "must retire historical replacements.*overlay:replace-old",
        ):
            MODULE._validate_historical_active_link_removals(
                historical_model,
                current_model,
                release_sha="a" * 40,
            )

    def test_release_validation_preserves_all_historical_removed_links(
        self,
    ) -> None:
        removal = removed("retired", "remove-retired")
        self.write_manifest(manifest("keep", removed_links=[removal]))
        older_sha = self.commit("Publish removal history")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        self.write_manifest(manifest("keep"))
        newer_sha = self.commit("Publish manifest without removal history")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                f"from release {older_sha}: remove-retired",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

    def test_release_validation_allows_exact_historical_removal_restoration(
        self,
    ) -> None:
        removal = removed("retired", "remove-retired")
        self.assertFalse(removal["legacy"])
        self.write_manifest(manifest("keep", removed_links=[removal]))
        older_sha = self.commit("Publish removal history")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        self.write_manifest(manifest("keep"))
        newer_sha = self.commit("Publish manifest without removal history")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        self.write_manifest(manifest("keep", removed_links=[removal]))
        self.commit("Restore historical removal")
        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            redirect_stdout(StringIO()),
        ):
            result = MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

        self.assertEqual(result, 0)

    def test_release_validation_rejects_conflicting_historical_removal(
        self,
    ) -> None:
        removal = removed("retired", "remove-retired")
        self.write_manifest(manifest("keep", removed_links=[removal]))
        older_sha = self.commit("Publish original removal history")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        conflicting_removal = dict(removal)
        conflicting_removal["source"] = "personal_codex/skills/changed-retired"
        self.write_manifest(
            manifest("keep", removed_links=[conflicting_removal])
        )
        newer_sha = self.commit("Publish conflicting removal history")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]
        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                f"{older_sha} and {newer_sha}: remove-retired",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

    def test_release_baseline_uses_commit_order_not_publish_time(self) -> None:
        self.write_manifest(manifest("keep", "retired"))
        older_sha = self.commit("Publish initial manifest")
        older_tag = f"personal-codex-20260715-020000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        removal = removed("retired", "remove-retired")
        self.write_manifest(manifest("keep", removed_links=[removal]))
        newer_sha = self.commit("Publish removal history")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)

        self.write_manifest(manifest("keep", "retired"))
        self.commit("Illegally discard removal history")
        releases = [
            complete_release(
                older_sha,
                tag=older_tag,
                published_at="2026-07-15T02:00:00Z",
            ),
            complete_release(
                newer_sha,
                tag=newer_tag,
                published_at="2026-07-15T01:00:00Z",
            ),
        ]

        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "removed link history changed or disappeared",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    MANIFEST.as_posix(),
                    "--release-repo",
                    "owner/repo",
                ]
            )

    def test_release_baseline_rejects_incomparable_release_commits(self) -> None:
        self.write_manifest(manifest("keep"))
        self.commit("Add common manifest")

        self.git("switch", "-c", "left-release")
        (self.repo / "left.txt").write_text("left\n", encoding="utf-8")
        left_sha = self.commit("Add left release")
        left_tag = f"personal-codex-20260715-010000-{left_sha[:7]}"
        self.git("tag", left_tag, left_sha)

        self.git("switch", "main")
        (self.repo / "right.txt").write_text("right\n", encoding="utf-8")
        right_sha = self.commit("Add right release")
        right_tag = f"personal-codex-20260715-020000-{right_sha[:7]}"
        self.git("tag", right_tag, right_sha)
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgSign=false",
            "merge",
            "--no-ff",
            "left-release",
            "-m",
            "Merge release histories",
        )
        releases = [
            complete_release(left_sha, tag=left_tag),
            complete_release(right_sha, tag=right_tag),
        ]

        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "do not have a single descendant baseline",
            ),
        ):
            MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

    def test_manifest_baseline_ignores_commit_replacement_ref(self) -> None:
        original = manifest("original")
        self.write_manifest(original)
        baseline_sha = self.commit("Add original manifest")
        self.write_manifest(manifest("replacement"))
        replacement_sha = self.commit("Add replacement manifest")
        self.git("replace", baseline_sha, replacement_sha)

        loaded = MODULE._manifest_at_ref(self.repo, baseline_sha, MANIFEST)

        self.assertEqual(loaded, original)

    def test_release_baseline_requires_token_and_complete_release(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            with self.assertRaisesRegex(MODULE.ValidationError, "GITHUB_TOKEN"):
                MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), mock.patch.object(
            MODULE,
            "_iter_github_releases",
            return_value=iter([]),
        ):
            with self.assertRaisesRegex(MODULE.ValidationError, "no complete"):
                MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

    def test_release_baseline_rejects_missing_or_mismatched_tag(self) -> None:
        self.write_manifest(manifest("keep"))
        baseline_sha = self.commit("Add manifest")
        missing_tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        release = complete_release(baseline_sha, tag=missing_tag)

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), self.release_patch(
            release
        ):
            with self.assertRaisesRegex(MODULE.ValidationError, "unavailable locally"):
                MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

        (self.repo / "README.md").write_text("current\n", encoding="utf-8")
        current_sha = self.commit("Advance history")
        tag = f"personal-codex-20260715-010000-{baseline_sha[:7]}"
        self.git("tag", tag, current_sha)
        release = complete_release(baseline_sha, tag=tag)
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), self.release_patch(
            release
        ):
            with self.assertRaisesRegex(MODULE.ValidationError, "resolves to"):
                MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

    def test_release_baseline_rejects_non_ancestor(self) -> None:
        self.write_manifest(manifest("current"))
        self.commit("Add current history")
        self.git("switch", "--orphan", "release-history")
        self.write_manifest(manifest("released"))
        baseline_sha = self.commit("Add unrelated release history")
        tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        self.git("tag", tag, baseline_sha)
        self.git("switch", "main")
        release = complete_release(baseline_sha, tag=tag)

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), self.release_patch(
            release
        ):
            with self.assertRaisesRegex(MODULE.ValidationError, "not an ancestor"):
                MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

    def test_release_baseline_rejects_missing_manifest(self) -> None:
        (self.repo / "README.md").write_text("released\n", encoding="utf-8")
        baseline_sha = self.commit("Add release without manifest")
        tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        self.git("tag", tag, baseline_sha)
        self.write_manifest(manifest("current"))
        self.commit("Add current manifest")
        release = complete_release(baseline_sha, tag=tag)

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), self.release_patch(
            release
        ):
            with self.assertRaisesRegex(MODULE.ValidationError, "does not contain"):
                MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

    def test_release_history_rejects_older_complete_release_without_manifest(
        self,
    ) -> None:
        (self.repo / "README.md").write_text("old release\n", encoding="utf-8")
        older_sha = self.commit("Add historical release without manifest")
        older_tag = f"personal-codex-20260715-000000-{older_sha[:7]}"
        self.git("tag", older_tag, older_sha)

        self.write_manifest(manifest("current"))
        newer_sha = self.commit("Add current release manifest")
        newer_tag = f"personal-codex-20260715-010000-{newer_sha[:7]}"
        self.git("tag", newer_tag, newer_sha)
        releases = [
            complete_release(older_sha, tag=older_tag),
            complete_release(newer_sha, tag=newer_tag),
        ]

        with (
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}),
            mock.patch.object(
                MODULE,
                "_iter_github_releases",
                side_effect=lambda *_args: iter(releases),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                f"complete release {older_sha} does not contain",
            ),
        ):
            MODULE._release_baseline(self.repo, "owner/repo", MANIFEST)

    def test_illegal_removal_stays_blocked_after_unrelated_commit(self) -> None:
        self.write_manifest(manifest("keep", "retired"))
        baseline_sha = self.commit("Add released manifest")
        tag = f"personal-codex-20260715-000000-{baseline_sha[:7]}"
        self.git("tag", tag, baseline_sha)
        release = complete_release(baseline_sha, tag=tag)
        self.write_manifest(manifest("keep"))
        self.commit("Remove link without history")

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), self.release_patch(
            release
        ):
            with self.assertRaisesRegex(MODULE.ValidationError, "requires one new matching"):
                MODULE.main(
                    [
                        "--repo-root",
                        str(self.repo),
                        "--manifest",
                        MANIFEST.as_posix(),
                        "--release-repo",
                        "owner/repo",
                    ]
                )

        (self.repo / "README.md").write_text("unrelated\n", encoding="utf-8")
        self.commit("Add unrelated change")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}), self.release_patch(
            release
        ), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(MODULE.ValidationError, "requires one new matching"):
                MODULE.main(
                    [
                        "--repo-root",
                        str(self.repo),
                        "--manifest",
                        MANIFEST.as_posix(),
                        "--release-repo",
                        "owner/repo",
                    ]
                )

    def test_release_workflows_use_release_repository_baseline(self) -> None:
        workflows = [REPO_ROOT / ".github/workflows/release.yml"]
        scheduled = REPO_ROOT / ".github/workflows/scheduled-sync-release.yml"
        if scheduled.is_file():
            workflows.append(scheduled)
        for workflow in workflows:
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertIn('--release-repo "$GITHUB_REPOSITORY"', text)
                self.assertIn("GITHUB_TOKEN: ${{ github.token }}", text)
                self.assertNotIn("github.event.before", text)
                self.assertNotIn("--base-ref HEAD^", text)

if __name__ == "__main__":
    unittest.main()
