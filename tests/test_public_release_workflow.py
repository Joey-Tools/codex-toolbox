from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


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
        "assets": [
            {
                "id": archive_id,
                "name": f"personal-codex-{sha}.tar.gz",
                "size": 1,
                "state": asset_state,
            },
            {
                "id": checksum_id,
                "name": f"personal-codex-{sha}.sha256",
                "size": 1,
                "state": asset_state,
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
        initial_release: dict[str, object],
        *,
        final_release: dict[str, object] | None = None,
    ) -> tuple[list[tuple[str, str]], SystemExit | None]:
        calls: list[tuple[str, str]] = []

        class FakeResponse:
            def __init__(self, payload: object) -> None:
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
            self.assertEqual(timeout, 30)
            method = request.get_method()
            url = request.full_url
            calls.append((method, url))
            if method == "GET" and "/releases?per_page=" in url:
                return FakeResponse([initial_release])
            if method == "DELETE" and "/releases/assets/" in url:
                return FakeResponse(None)
            if method == "POST" and url.startswith("https://uploads.github.com/"):
                asset_name = url.rsplit("?name=", 1)[1]
                return FakeResponse(
                    {
                        "id": 999,
                        "name": asset_name,
                        "state": "uploaded",
                    }
                )
            if method == "GET" and url.endswith(f"/releases/{self.RELEASE_ID}"):
                if final_release is None:
                    raise AssertionError("unexpected final release lookup")
                return FakeResponse(final_release)
            if method == "PATCH" and url.endswith(f"/releases/{self.RELEASE_ID}"):
                return FakeResponse({"draft": False})
            raise AssertionError(f"unexpected request: {method} {url}")

        exit_error = None
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            dist = temp_root / "dist"
            dist.mkdir()
            (dist / f"personal-codex-{self.SHA}.tar.gz").write_bytes(b"archive")
            (dist / f"personal-codex-{self.SHA}.sha256").write_bytes(b"checksum")
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
                tag = (
                    "personal-codex-20260715-000000-"
                    f"{self.SHA[:prefix_length]}"
                )
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
                    "personal-codex-20260715-000000-"
                    f"{self.SHA[:prefix_length]}"
                )

                calls, exit_error = self.run_publish_script(initial_release)

                self.assertIsNotNone(exit_error)
                assert exit_error is not None
                self.assertEqual(exit_error.code, 0)
                self.assertFalse(
                    any(
                        method in {"DELETE", "PATCH", "POST"}
                        for method, _url in calls
                    )
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

    def test_published_release_does_not_accept_pending_assets_by_name(self) -> None:
        initial_release = self.release(draft=False, asset_state="new")

        calls, exit_error = self.run_publish_script(initial_release)

        self.assertIsNotNone(exit_error)
        assert exit_error is not None
        self.assertIn("matching assets not uploaded", str(exit_error))
        self.assertFalse(any(method == "PATCH" for method, _url in calls))

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
        self.assertFalse(any(method == "PATCH" for method, _url in calls))


class PublicReleaseWorkflowContractTests(unittest.TestCase):
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
