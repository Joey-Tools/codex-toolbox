from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock
from urllib.error import HTTPError


REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PAYLOAD = b"archive"
CHECKSUM_PAYLOAD = b"checksum"
DEFAULT_IMMUTABLE_RELEASES = object()


def asset_content(payload: bytes) -> dict[str, object]:
    return {
        "size": len(payload),
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
    }


def complete_release(
    sha: str,
    *,
    tag: str | None = None,
    draft: bool = False,
    asset_state: str = "uploaded",
    archive_id: int = 101,
    checksum_id: int = 102,
) -> dict[str, object]:
    return {
        "tag_name": tag or f"personal-codex-20260715-000000-{sha[:7]}",
        "target_commitish": sha,
        "draft": draft,
        "prerelease": False,
        "immutable": not draft,
        "assets": [
            {
                "id": archive_id,
                "name": f"personal-codex-{sha}.tar.gz",
                "state": asset_state,
                **asset_content(ARCHIVE_PAYLOAD),
            },
            {
                "id": checksum_id,
                "name": f"personal-codex-{sha}.sha256",
                "state": asset_state,
                **asset_content(CHECKSUM_PAYLOAD),
            },
        ],
    }


class ReleaseWorkflowAssetRetryTests(unittest.TestCase):
    SHA = "a" * 40
    RELEASE_ID = 123

    @classmethod
    def release(
        cls, *, draft: bool, asset_state: str = "uploaded"
    ) -> dict[str, object]:
        release = complete_release(
            cls.SHA,
            draft=draft,
            asset_state=asset_state,
            archive_id=101,
            checksum_id=102,
        )
        release["id"] = cls.RELEASE_ID
        return release

    @staticmethod
    def publish_script() -> str:
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        step_marker = "      - name: Publish GitHub release\n"
        heredoc_marker = "          python3 - <<'PY'\n"
        step = workflow.split(step_marker, 1)[1]
        script = step.split(heredoc_marker, 1)[1].split("\n          PY", 1)[0]
        return textwrap.dedent(script)

    def run_publish_script(
        self,
        initial_release: dict[str, object] | list[dict[str, object]],
        *,
        final_release: dict[str, object] | None = None,
        published_release: dict[str, object] | None = None,
        immutable_releases: object = DEFAULT_IMMUTABLE_RELEASES,
        immutable_releases_error: Exception | None = None,
    ) -> tuple[list[tuple[str, str]], SystemExit | None]:
        calls: list[tuple[str, str]] = []
        initial_releases = (
            initial_release if isinstance(initial_release, list) else [initial_release]
        )
        release_lookup_count = 0

        class FakeResponse:
            def __init__(self, payload: object) -> None:
                if isinstance(payload, bytes):
                    self.body = payload
                else:
                    self.body = (
                        b"" if payload is None else json.dumps(payload).encode("utf-8")
                    )

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def read(self) -> bytes:
                return self.body

        def fake_urlopen(request: object, *, timeout: int) -> FakeResponse:
            nonlocal release_lookup_count
            self.assertEqual(timeout, 30)
            method = request.get_method()
            url = request.full_url
            calls.append((method, url))
            if method == "GET" and "/releases?per_page=" in url:
                return FakeResponse(initial_releases if url.endswith("&page=1") else [])
            if method == "GET" and url.endswith("/immutable-releases"):
                self.assertEqual(
                    request.get_header("X-github-api-version"),
                    "2026-03-10",
                )
                if immutable_releases_error is not None:
                    raise immutable_releases_error
                settings = (
                    {"enabled": True}
                    if immutable_releases is DEFAULT_IMMUTABLE_RELEASES
                    else immutable_releases
                )
                return FakeResponse(settings)
            if method == "DELETE" and "/releases/assets/" in url:
                return FakeResponse(None)
            if method == "POST" and url.startswith("https://uploads.github.com/"):
                asset_name = url.rsplit("?name=", 1)[1]
                expected_payload = (
                    CHECKSUM_PAYLOAD
                    if asset_name.endswith(".sha256")
                    else ARCHIVE_PAYLOAD
                )
                self.assertEqual(request.data, expected_payload)
                return FakeResponse(
                    {
                        "id": 999,
                        "name": asset_name,
                        "state": "uploaded",
                    }
                )
            if method == "GET" and url.endswith(f"/releases/{self.RELEASE_ID}"):
                release_lookup_count += 1
                if release_lookup_count == 1:
                    if final_release is None:
                        raise AssertionError("unexpected final release lookup")
                    return FakeResponse(final_release)
                if release_lookup_count == 2:
                    if published_release is not None:
                        return FakeResponse(published_release)
                    if final_release is None:
                        raise AssertionError("unexpected published release lookup")
                    default_published_release = dict(final_release)
                    default_published_release["draft"] = False
                    default_published_release["immutable"] = True
                    return FakeResponse(default_published_release)
                raise AssertionError("unexpected extra release lookup")
            if method == "PATCH" and url.endswith(f"/releases/{self.RELEASE_ID}"):
                return FakeResponse({"untrusted": True})
            raise AssertionError(f"unexpected request: {method} {url}")

        exit_error = None
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            dist = temp_root / "dist"
            dist.mkdir()
            (dist / f"personal-codex-{self.SHA}.tar.gz").write_bytes(ARCHIVE_PAYLOAD)
            (dist / f"personal-codex-{self.SHA}.sha256").write_bytes(CHECKSUM_PAYLOAD)
            try:
                os.chdir(temp_root)
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "GITHUB_REPOSITORY": "owner/repo",
                            "GITHUB_SHA": self.SHA,
                            "GITHUB_TOKEN": "token",
                        },
                    ),
                    mock.patch(
                        "urllib.request.urlopen",
                        side_effect=fake_urlopen,
                    ),
                    redirect_stdout(StringIO()),
                ):
                    try:
                        exec(
                            compile(
                                self.publish_script(),
                                ".github/workflows/release.yml",
                                "exec",
                            ),
                            {"__name__": "__main__"},
                        )
                    except SystemExit as error:
                        exit_error = error
            finally:
                os.chdir(original_cwd)
        return calls, exit_error

    def assert_full_replacement(
        self,
        calls: list[tuple[str, str]],
        *,
        deleted_asset_ids: list[int],
    ) -> None:
        expected_deletions = [
            (
                "DELETE",
                f"https://api.github.com/repos/owner/repo/releases/assets/{asset_id}",
            )
            for asset_id in deleted_asset_ids
        ]
        expected_uploads = [
            (
                "POST",
                "https://uploads.github.com/repos/owner/repo/releases/123/assets"
                f"?name=personal-codex-{self.SHA}.tar.gz",
            ),
            (
                "POST",
                "https://uploads.github.com/repos/owner/repo/releases/123/assets"
                f"?name=personal-codex-{self.SHA}.sha256",
            ),
        ]
        repair_mutations = [
            (method, url)
            for method, url in calls
            if method == "DELETE" or url.startswith("https://uploads.github.com/")
        ]
        self.assertEqual(
            repair_mutations,
            [
                *expected_deletions,
                *expected_uploads,
            ],
        )
        self.assertEqual(sum(method == "PATCH" for method, _url in calls), 1)

    def assert_no_mutations(self, calls: list[tuple[str, str]]) -> None:
        self.assertFalse(
            any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
        )

    def assert_failed(self, exit_error: SystemExit | None) -> SystemExit:
        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertNotEqual(exit_error.code, 0)
        return exit_error

    def test_retry_replaces_pending_pair_in_full(self) -> None:
        initial_release = self.release(draft=True)
        initial_release["assets"][0]["state"] = "new"
        final_release = self.release(draft=True)

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assert_full_replacement(calls, deleted_asset_ids=[101, 102])

    def test_mutation_preflight_uses_immutable_releases_api_version(self) -> None:
        initial_release = self.release(draft=True)
        final_release = self.release(draft=True)

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assertIn(
            (
                "GET",
                "https://api.github.com/repos/owner/repo/immutable-releases",
            ),
            calls,
        )

    def test_mutation_preflight_fails_closed_without_mutation(self) -> None:
        failure_cases = (
            ("disabled", {"enabled": False}, None, "enabled=true"),
            ("integer-one", {"enabled": 1}, None, "enabled=true"),
            ("missing-enabled", {}, None, "enabled=true"),
            ("non-object", [], None, "enabled=true"),
            ("invalid-json", b"not-json", None, "not valid JSON"),
            ("not-found", None, 404, "HTTP 404"),
            ("server-error", None, 500, "HTTP 500"),
        )
        release_states = (
            ("new-release", []),
            ("draft-repair", self.release(draft=True)),
        )
        for case, payload, status, error_text in failure_cases:
            for release_state, initial_release in release_states:
                with self.subTest(case=case, release_state=release_state):
                    error = None
                    if status is not None:
                        error = HTTPError(
                            "https://api.github.com/repos/owner/repo/immutable-releases",
                            status,
                            "preflight failed",
                            None,
                            None,
                        )
                    calls, exit_error = self.run_publish_script(
                        initial_release,
                        immutable_releases=payload,
                        immutable_releases_error=error,
                    )

                    failure = self.assert_failed(exit_error)
                    self.assertIn(error_text, str(failure))
                    self.assert_no_mutations(calls)

    def test_retry_replaces_partial_uploaded_pair_in_full(self) -> None:
        initial_release = self.release(draft=True)
        initial_release["assets"] = [initial_release["assets"][0]]
        final_release = self.release(draft=True)

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assert_full_replacement(calls, deleted_asset_ids=[101])

    def test_retry_replaces_duplicate_matching_asset_set_in_full(self) -> None:
        initial_release = self.release(draft=True)
        initial_release["assets"].append(
            {
                "id": 103,
                "name": f"personal-codex-{self.SHA}.tar.gz",
                "size": 1,
                "state": "new",
            }
        )
        final_release = self.release(draft=True)

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assert_full_replacement(calls, deleted_asset_ids=[101, 102, 103])

    def test_retry_replaces_other_sha_pending_asset_set_in_full(self) -> None:
        other_sha = "b" * 40
        initial_release = self.release(draft=True)
        initial_release["assets"].append(
            {
                "id": 104,
                "name": f"personal-codex-{other_sha}.sha256",
                "size": 1,
                "state": "new",
            }
        )
        final_release = self.release(draft=True)

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assert_full_replacement(calls, deleted_asset_ids=[101, 102, 104])

    def test_retry_replaces_other_sha_uploaded_asset_set_in_full(self) -> None:
        other_sha = "b" * 40
        initial_release = self.release(draft=True)
        initial_release["assets"].append(
            {
                "id": 104,
                "name": f"personal-codex-{other_sha}.tar.gz",
                "size": 1,
                "state": "uploaded",
            }
        )
        final_release = self.release(draft=True)

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assert_full_replacement(calls, deleted_asset_ids=[101, 102, 104])

    def test_complete_uploaded_draft_pair_is_replaced_in_full(self) -> None:
        initial_release = self.release(draft=True)
        final_release = self.release(draft=True)

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assert_full_replacement(calls, deleted_asset_ids=[101, 102])

    def test_long_valid_draft_tag_prefixes_are_repaired_in_full(self) -> None:
        for prefix_length in (8, 40):
            with self.subTest(prefix_length=prefix_length):
                tag = f"personal-codex-20260715-000000-{self.SHA[:prefix_length]}"
                initial_release = self.release(draft=True)
                initial_release["tag_name"] = tag
                final_release = self.release(draft=True)
                final_release["tag_name"] = tag

                calls, exit_error = self.run_publish_script(
                    initial_release,
                    final_release=final_release,
                )

                self.assertIsNone(exit_error)
                self.assert_full_replacement(calls, deleted_asset_ids=[101, 102])

    def test_long_valid_published_tag_prefixes_are_reused_read_only(self) -> None:
        for prefix_length in (8, 40):
            with self.subTest(prefix_length=prefix_length):
                initial_release = self.release(draft=False)
                initial_release["tag_name"] = (
                    f"personal-codex-20260715-000000-{self.SHA[:prefix_length]}"
                )

                calls, exit_error = self.run_publish_script(initial_release)

                self.assertIsNotNone(exit_error)
                assert exit_error is not None
                self.assertEqual(exit_error.code, 0)
                self.assertFalse(
                    any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
                )

    def test_published_release_with_exact_content_is_reused_read_only(self) -> None:
        initial_release = self.release(draft=False)
        initial_release["assets"].append(
            {
                "id": True,
                "name": "release-notes.txt",
                "size": False,
                "state": "new",
            }
        )

        calls, exit_error = self.run_publish_script(initial_release)

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertEqual(exit_error.code, 0)
        self.assert_no_mutations(calls)

    def test_published_release_requires_valid_release_id_without_mutation(
        self,
    ) -> None:
        cases = (
            ("missing", None),
            ("zero", 0),
            ("boolean", True),
        )
        for name, invalid_id in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=False)
                if invalid_id is None:
                    initial_release.pop("id")
                else:
                    initial_release["id"] = invalid_id

                calls, exit_error = self.run_publish_script(initial_release)

                error = self.assert_failed(exit_error)
                self.assertIn("invalid release id", str(error))
                self.assert_no_mutations(calls)

    def test_published_release_requires_immutable_true_without_mutation(
        self,
    ) -> None:
        cases = (
            ("missing", None),
            ("false", False),
            ("integer-one", 1),
        )
        for name, invalid_immutable in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=False)
                if invalid_immutable is None:
                    initial_release.pop("immutable")
                else:
                    initial_release["immutable"] = invalid_immutable

                calls, exit_error = self.run_publish_script(initial_release)

                error = self.assert_failed(exit_error)
                self.assertIn("must be immutable", str(error))
                self.assert_no_mutations(calls)

    def test_published_release_requires_valid_unique_asset_ids_without_mutation(
        self,
    ) -> None:
        cases = (
            ("missing", None),
            ("zero", 0),
            ("boolean", True),
            ("string", "101"),
            ("duplicate", 102),
        )
        for name, invalid_id in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=False)
                archive_asset = initial_release["assets"][0]
                if invalid_id is None:
                    archive_asset.pop("id")
                else:
                    archive_asset["id"] = invalid_id

                calls, exit_error = self.run_publish_script(initial_release)

                error = self.assert_failed(exit_error)
                if name == "duplicate":
                    self.assertIn("duplicate matching asset ids", str(error))
                else:
                    self.assertIn("positive integer id", str(error))
                self.assert_no_mutations(calls)

    def test_published_release_requires_exact_asset_content_without_mutation(
        self,
    ) -> None:
        cases = (
            ("archive-size-missing", 0, "size", None, True, "invalid size"),
            ("archive-size-string", 0, "size", "7", False, "invalid size"),
            ("archive-size-boolean", 0, "size", True, False, "invalid size"),
            ("archive-size-negative", 0, "size", -1, False, "invalid size"),
            (
                "checksum-size-mismatch",
                1,
                "size",
                len(CHECKSUM_PAYLOAD) + 1,
                False,
                "size mismatch",
            ),
            ("archive-digest-missing", 0, "digest", None, True, "invalid digest"),
            (
                "archive-digest-non-string",
                0,
                "digest",
                123,
                False,
                "invalid digest",
            ),
            (
                "checksum-digest-malformed",
                1,
                "digest",
                "sha256:not-a-digest",
                False,
                "invalid digest",
            ),
            (
                "checksum-digest-wrong-algorithm",
                1,
                "digest",
                f"sha512:{'0' * 64}",
                False,
                "invalid digest",
            ),
            (
                "checksum-digest-mismatch",
                1,
                "digest",
                f"sha256:{'0' * 64}",
                False,
                "digest mismatch",
            ),
        )
        for name, asset_index, field, value, remove, error_text in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=False)
                release_asset = initial_release["assets"][asset_index]
                if remove:
                    release_asset.pop(field)
                else:
                    release_asset[field] = value

                calls, exit_error = self.run_publish_script(initial_release)

                error = self.assert_failed(exit_error)
                self.assertIn(error_text, str(error))
                self.assert_no_mutations(calls)

    def test_published_release_rejects_nonexact_matching_name_set_without_mutation(
        self,
    ) -> None:
        for name in ("missing", "other-sha", "duplicate"):
            with self.subTest(name=name):
                initial_release = self.release(draft=False)
                if name == "missing":
                    initial_release["assets"].pop()
                    error_text = "matching asset name mismatch"
                elif name == "other-sha":
                    initial_release["assets"].append(
                        {
                            "id": 103,
                            "name": f"personal-codex-{'b' * 40}.tar.gz",
                            "state": "uploaded",
                            **asset_content(ARCHIVE_PAYLOAD),
                        }
                    )
                    error_text = "unexpected uploaded assets"
                else:
                    duplicate_asset = dict(initial_release["assets"][0])
                    duplicate_asset["id"] = 103
                    initial_release["assets"].append(duplicate_asset)
                    error_text = "duplicate uploaded assets"

                calls, exit_error = self.run_publish_script(initial_release)

                error = self.assert_failed(exit_error)
                self.assertIn(error_text, str(error))
                self.assert_no_mutations(calls)

    def test_published_prerelease_does_not_anchor_formal_release(self) -> None:
        prerelease = self.release(draft=False)
        prerelease["prerelease"] = True
        formal_release = self.release(draft=False)

        calls, exit_error = self.run_publish_script([prerelease, formal_release])

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertEqual(exit_error.code, 0)
        self.assertFalse(
            any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
        )

    def test_prerelease_draft_fails_without_mutation(self) -> None:
        initial_release = self.release(draft=True)
        initial_release["prerelease"] = True

        calls, exit_error = self.run_publish_script(initial_release)

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertIn("must not be a prerelease", str(exit_error))
        self.assertFalse(
            any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
        )

    def test_formal_candidate_flags_must_be_boolean_without_mutation(self) -> None:
        cases = (
            ("missing-prerelease", "prerelease", None),
            ("string-prerelease", "prerelease", "false"),
            ("missing-draft", "draft", None),
            ("string-draft", "draft", "false"),
        )
        for name, field, invalid_value in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=True)
                if invalid_value is None:
                    initial_release.pop(field)
                else:
                    initial_release[field] = invalid_value

                calls, exit_error = self.run_publish_script(initial_release)

                self.assertIsNotNone(exit_error)
                assert exit_error is not None
                self.assertIn(f"invalid {field} flag", str(exit_error))
                self.assertFalse(
                    any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
                )

    def test_invalid_draft_tag_or_suffix_fails_before_asset_mutation(self) -> None:
        cases = (
            (
                "invalid-format",
                f"personal-codex-20260715-0000-{self.SHA[:7]}",
                "invalid release tag",
            ),
            (
                "wrong-sha-suffix",
                f"personal-codex-20260715-000000-{'b' * 7}",
                "tag suffix does not match target SHA",
            ),
            (
                "wrong-long-sha-prefix",
                f"personal-codex-20260715-000000-{self.SHA[:7]}b",
                "tag suffix does not match target SHA",
            ),
        )
        for name, tag_name, error_pattern in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=True)
                initial_release["tag_name"] = tag_name
                initial_release["assets"][0]["state"] = "new"

                calls, exit_error = self.run_publish_script(initial_release)

                self.assertIsNotNone(exit_error)
                assert exit_error is not None
                self.assertIn(error_pattern, str(exit_error))
                self.assertFalse(
                    any(
                        method in {"DELETE", "PATCH"}
                        or url.startswith("https://uploads.github.com/")
                        for method, url in calls
                    )
                )

    def test_final_identity_drift_fails_before_publish(self) -> None:
        drift_cases = (
            ("id", self.RELEASE_ID + 1),
            (
                "tag_name",
                f"personal-codex-20260715-000001-{self.SHA[:7]}",
            ),
            ("target_commitish", "b" * 40),
            ("draft", False),
            ("prerelease", True),
        )
        for field, drifted_value in drift_cases:
            with self.subTest(field=field):
                initial_release = self.release(draft=True)
                final_release = self.release(draft=True)
                final_release[field] = drifted_value

                calls, exit_error = self.run_publish_script(
                    initial_release,
                    final_release=final_release,
                )

                self.assertIsNotNone(exit_error)
                assert exit_error is not None
                self.assertIn("final release identity drift", str(exit_error))
                self.assertFalse(any(method == "PATCH" for method, _url in calls))

    def test_pre_publish_content_drift_fails_before_patch(self) -> None:
        cases = (
            ("size-missing", "invalid size"),
            ("size-mismatch", "size mismatch"),
            ("digest-missing", "invalid digest"),
            ("digest-mismatch", "digest mismatch"),
            ("duplicate-id", "duplicate matching asset ids"),
            ("pending", "matching assets not uploaded"),
        )
        for name, error_text in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=True)
                final_release = self.release(draft=True)
                archive_asset = final_release["assets"][0]
                if name == "size-missing":
                    archive_asset.pop("size")
                elif name == "size-mismatch":
                    archive_asset["size"] = len(ARCHIVE_PAYLOAD) + 1
                elif name == "digest-missing":
                    archive_asset.pop("digest")
                elif name == "digest-mismatch":
                    archive_asset["digest"] = f"sha256:{'0' * 64}"
                elif name == "duplicate-id":
                    archive_asset["id"] = 102
                else:
                    archive_asset["state"] = "new"

                calls, exit_error = self.run_publish_script(
                    initial_release,
                    final_release=final_release,
                )

                error = self.assert_failed(exit_error)
                self.assertIn(error_text, str(error))
                self.assertFalse(any(method == "PATCH" for method, _url in calls))

    def test_pre_publish_draft_does_not_require_immutable(self) -> None:
        initial_release = self.release(draft=True)
        final_release = self.release(draft=True)
        final_release.pop("immutable")

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNone(exit_error)
        self.assertEqual(sum(method == "PATCH" for method, _url in calls), 1)

    def test_post_publish_content_drift_is_detected_after_patch(self) -> None:
        cases = (
            ("size-missing", "invalid size"),
            ("size-mismatch", "size mismatch"),
            ("digest-missing", "invalid digest"),
            ("digest-mismatch", "digest mismatch"),
            ("duplicate-id", "duplicate matching asset ids"),
            ("pending", "matching assets not uploaded"),
        )
        for name, error_text in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=True)
                final_release = self.release(draft=True)
                published_release = self.release(draft=False)
                archive_asset = published_release["assets"][0]
                if name == "size-missing":
                    archive_asset.pop("size")
                elif name == "size-mismatch":
                    archive_asset["size"] = len(ARCHIVE_PAYLOAD) + 1
                elif name == "digest-missing":
                    archive_asset.pop("digest")
                elif name == "digest-mismatch":
                    archive_asset["digest"] = f"sha256:{'0' * 64}"
                elif name == "duplicate-id":
                    archive_asset["id"] = 102
                else:
                    archive_asset["state"] = "new"

                calls, exit_error = self.run_publish_script(
                    initial_release,
                    final_release=final_release,
                    published_release=published_release,
                )

                error = self.assert_failed(exit_error)
                self.assertIn(error_text, str(error))
                self.assertEqual(
                    sum(method == "PATCH" for method, _url in calls),
                    1,
                )

    def test_post_publish_immutable_drift_is_detected_after_patch(self) -> None:
        cases = (
            ("missing", None),
            ("false", False),
            ("integer-one", 1),
        )
        for name, invalid_immutable in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=True)
                final_release = self.release(draft=True)
                published_release = self.release(draft=False)
                if invalid_immutable is None:
                    published_release.pop("immutable")
                else:
                    published_release["immutable"] = invalid_immutable

                calls, exit_error = self.run_publish_script(
                    initial_release,
                    final_release=final_release,
                    published_release=published_release,
                )

                error = self.assert_failed(exit_error)
                self.assertIn("must be immutable", str(error))
                self.assertEqual(
                    sum(method == "PATCH" for method, _url in calls),
                    1,
                )

    def test_published_prerelease_drift_is_detected_after_patch(self) -> None:
        initial_release = self.release(draft=True)
        final_release = self.release(draft=True)
        published_release = self.release(draft=False)
        published_release["prerelease"] = True

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
            published_release=published_release,
        )

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertIn("published release identity drift", str(exit_error))
        self.assertEqual(sum(method == "PATCH" for method, _url in calls), 1)

    def test_final_check_rejects_uploaded_pair_with_extra_pending_match(self) -> None:
        initial_release = self.release(draft=True)
        final_release = self.release(draft=True)
        final_release["assets"].append(
            {
                "id": 103,
                "name": f"personal-codex-{self.SHA}.tar.gz",
                "size": 1,
                "state": "new",
            }
        )

        calls, exit_error = self.run_publish_script(
            initial_release,
            final_release=final_release,
        )

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertIn("matching assets not uploaded", str(exit_error))
        self.assertFalse(any(method == "PATCH" for method, _url in calls))

    def test_retry_fails_before_mutation_when_matching_asset_id_is_invalid(
        self,
    ) -> None:
        cases = (("missing", None), ("zero", 0), ("boolean", True))
        for name, invalid_id in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=True)
                repair_asset = initial_release["assets"][0]
                if invalid_id is None:
                    repair_asset.pop("id")
                else:
                    repair_asset["id"] = invalid_id

                calls, exit_error = self.run_publish_script(initial_release)

                self.assertIsNotNone(exit_error)
                assert exit_error is not None
                self.assertRegex(
                    str(exit_error),
                    r"matching asset without a (?:valid|positive) id",
                )
                self.assertFalse(
                    any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
                )

    def test_retry_fails_before_mutation_when_matching_asset_ids_repeat(self) -> None:
        initial_release = self.release(draft=True)
        initial_release["assets"][1]["id"] = 101

        calls, exit_error = self.run_publish_script(initial_release)

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertIn("duplicate matching asset ids", str(exit_error))
        self.assertFalse(
            any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
        )

    def test_draft_release_id_must_be_positive_integer_before_mutation(self) -> None:
        cases = (("missing", None), ("zero", 0), ("boolean", True))
        for name, invalid_id in cases:
            with self.subTest(name=name):
                initial_release = self.release(draft=True)
                if invalid_id is None:
                    initial_release.pop("id")
                else:
                    initial_release["id"] = invalid_id

                calls, exit_error = self.run_publish_script(initial_release)

                self.assertIsNotNone(exit_error)
                assert exit_error is not None
                self.assertIn("invalid release id", str(exit_error))
                self.assertFalse(
                    any(method in {"DELETE", "PATCH", "POST"} for method, _url in calls)
                )

    def test_published_release_does_not_accept_pending_assets_by_name(self) -> None:
        initial_release = self.release(draft=False, asset_state="new")

        calls, exit_error = self.run_publish_script(initial_release)

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertIn("matching assets not uploaded", str(exit_error))
        self.assert_no_mutations(calls)

    def test_published_release_rejects_uploaded_pair_with_pending_match(self) -> None:
        initial_release = self.release(draft=False)
        initial_release["assets"].append(
            {
                "id": 103,
                "name": f"personal-codex-{self.SHA}.sha256",
                "size": 1,
                "state": "new",
            }
        )

        calls, exit_error = self.run_publish_script(initial_release)

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertIn("matching assets not uploaded", str(exit_error))
        self.assert_no_mutations(calls)


class PublicReleaseWorkflowContractTests(unittest.TestCase):
    def test_publish_step_hashes_and_uploads_one_cached_asset_snapshot(self) -> None:
        publish_script = ReleaseWorkflowAssetRetryTests.publish_script()

        self.assertIn("asset_file.read(1024 * 1024)", publish_script)
        self.assertIn('"data": bytes(content)', publish_script)
        self.assertIn(
            'data=expected_asset_content[asset.name]["data"]',
            publish_script,
        )
        self.assertNotIn("data=asset.read_bytes()", publish_script)

    def test_release_workflow_extracts_the_verified_archive_snapshot(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(workflow.count("module.verify_and_extract_archive("), 2)
        self.assertEqual(workflow.count("module.bind_archive_workspace(dist)"), 2)
        self.assertEqual(workflow.count("workspace=workspace"), 2)
        self.assertEqual(workflow.count("module.read_expected_release_file("), 2)
        self.assertEqual(workflow.count("release_expectation[0][1].entries"), 2)
        self.assertNotIn("module.verify_checksum(", workflow)
        self.assertNotIn("module.safe_extract_archive(", workflow)
        self.assertNotIn("module.validate_release_tree(", workflow)
        self.assertNotIn("runner.read_text(", workflow)


if __name__ == "__main__":
    unittest.main()
