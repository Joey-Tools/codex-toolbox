from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
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
) -> dict[str, object]:
    return {
        "tag_name": tag or f"personal-codex-20260715-000000-{sha[:7]}",
        "target_commitish": sha,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published_at,
        "assets": [
            {
                "name": f"personal-codex-{sha}.tar.gz",
                "state": asset_state,
            },
            {
                "name": f"personal-codex-{sha}.sha256",
                "state": asset_state,
            },
        ],
    }


class ReleaseManifestBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo = Path(self.temporary_directory.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")

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
        mismatched_assets = complete_release(sha)
        mismatched_assets["assets"][1]["name"] = (
            f"personal-codex-{'c' * 40}.sha256"
        )
        pending_assets = complete_release(sha, asset_state="new")
        first_page = [draft, prerelease, mismatched_assets, pending_assets] + [draft] * 96
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
            self.assertEqual(
                MODULE._complete_release_identities("owner/repo", "token"),
                [
                    (older["tag_name"], older["target_commitish"]),
                    (expected["tag_name"], sha),
                ],
            )

        self.assertEqual(request_json.call_count, 2)
        self.assertIn("page=1", request_json.call_args_list[0].args[0])
        self.assertIn("page=2", request_json.call_args_list[1].args[0])

    def test_complete_release_identity_uses_asset_sha_and_rejects_conflicts(
        self,
    ) -> None:
        sha = "a" * 40
        release = complete_release(sha)
        release["target_commitish"] = "main"
        self.assertEqual(
            MODULE._complete_release_identity(release),
            (release["published_at"], release["tag_name"], sha),
        )

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
