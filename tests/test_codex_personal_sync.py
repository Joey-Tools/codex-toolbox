from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
PACKAGE_SCRIPT_PATH = REPO_ROOT / "scripts" / "build_personal_codex_package.py"
SPEC = importlib.util.spec_from_file_location("codex_personal_sync", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


SHA1 = "1" * 40
SHA2 = "2" * 40
SHA3 = "3" * 40


def write_minimal_release(
    release_root: Path,
    *,
    agent_text: str = "agent\n",
    skill_text: str = "---\nname: example\n---\n",
) -> None:
    personal_root = release_root / "personal_codex"
    skill_root = personal_root / "skills" / "example-skill"
    bin_root = personal_root / "bin"
    scripts_root = release_root / "scripts"
    personal_root.mkdir(parents=True)
    skill_root.mkdir(parents=True)
    bin_root.mkdir(parents=True)
    scripts_root.mkdir(parents=True)
    (personal_root / "AGENTS.md").write_text(agent_text, encoding="utf-8")
    (skill_root / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (bin_root / "example-tool").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (scripts_root / "codex_personal_sync.py").write_text(
        "#!/usr/bin/env python3\n",
        encoding="utf-8",
    )
    (personal_root / "sync-manifest.json").write_text(
        """
{
  "version": 1,
  "links": [
    {
      "source": "personal_codex/AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    },
    {
      "source": "personal_codex/skills/example-skill",
      "target": "skills/example-skill",
      "kind": "skill"
    },
    {
      "source": "personal_codex/bin/example-tool",
      "target": "bin/example-tool",
      "kind": "file"
    },
    {
      "source": "scripts/codex_personal_sync.py",
      "target": "bin/codex-personal-sync",
      "kind": "file"
    }
  ],
  "reference_only": []
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_agent_only_release(release_root: Path, *, agent_text: str = "agent\n") -> None:
    personal_root = release_root / "personal_codex"
    personal_root.mkdir(parents=True)
    (personal_root / "AGENTS.md").write_text(agent_text, encoding="utf-8")
    (personal_root / "sync-manifest.json").write_text(
        """
{
  "version": 1,
  "links": [
    {
      "source": "personal_codex/AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    }
  ],
  "reference_only": []
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_rules_release(release_root: Path, *, agent_text: str = "agent\n") -> None:
    personal_root = release_root / "personal_codex"
    rules_root = personal_root / "rules"
    personal_root.mkdir(parents=True)
    rules_root.mkdir()
    (personal_root / "AGENTS.md").write_text(agent_text, encoding="utf-8")
    (rules_root / "example-rule").write_text("rule\n", encoding="utf-8")
    (personal_root / "sync-manifest.json").write_text(
        """
{
  "version": 1,
  "links": [
    {
      "source": "personal_codex/AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    },
    {
      "source": "personal_codex/rules/example-rule",
      "target": "rules/example-rule",
      "kind": "file"
    }
  ],
  "reference_only": []
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_private_skill_only_release(
    release_root: Path,
    *,
    private_skill_text: str = "---\nname: private-skill\n---\n",
) -> None:
    personal_root = release_root / "personal_codex"
    skill_root = personal_root / "skills" / "private-skill"
    personal_root.mkdir(parents=True)
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(private_skill_text, encoding="utf-8")
    (personal_root / "sync-manifest.json").write_text(
        """
{
  "version": 1,
  "owner": "private",
  "base_release": {
    "repo": "Joey-Tools/codex-toolbox"
  },
  "links": [
    {
      "source": "personal_codex/skills/private-skill",
      "target": "skills/private-skill",
      "kind": "skill"
    }
  ],
  "reference_only": []
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_private_agent_release(
    release_root: Path,
    *,
    agent_text: str = "private\n",
) -> None:
    personal_root = release_root / "personal_codex"
    personal_root.mkdir(parents=True)
    (personal_root / "AGENTS.md").write_text(agent_text, encoding="utf-8")
    (personal_root / "sync-manifest.json").write_text(
        """
{
  "version": 1,
  "owner": "private",
  "base_release": {
    "repo": "Joey-Tools/codex-toolbox"
  },
  "links": [
    {
      "source": "personal_codex/AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    }
  ],
  "reference_only": []
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def current_target(home: Path) -> str:
    return (home / "personal-sync" / "current").readlink().as_posix()


def write_scheduler_runner(home: Path) -> Path:
    runner = home / "bin" / "codex-personal-sync"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    return runner


class CodexPersonalSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="codex-personal-sync.")
        self.root = Path(self.tmpdir.name)
        self.user_home = self.root / "home"
        self.path_home_patch = mock.patch.object(MODULE.Path, "home", return_value=self.user_home)
        self.path_home_patch.start()

    def tearDown(self) -> None:
        self.path_home_patch.stop()
        self.tmpdir.cleanup()

    def run_quietly(self, callback, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return callback(*args, **kwargs)

    def test_release_repo_is_required_without_default_environment(self) -> None:
        parser = MODULE.build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["install"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["install-private"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["install-scheduler"])

    def test_default_release_repo_can_be_overridden_by_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CODEX_PERSONAL_SYNC_DEFAULT_REPO": "ExampleOrg/example-codex"},
        ):
            parser = MODULE.build_parser()

        install_args = parser.parse_args(["install"])
        install_private_args = parser.parse_args(["install-private"])
        scheduler_args = parser.parse_args(["install-scheduler"])

        self.assertEqual(install_args.repo, "ExampleOrg/example-codex")
        self.assertEqual(install_private_args.repo, "ExampleOrg/example-codex")
        self.assertEqual(install_private_args.base_repo, "Joey-Tools/codex-toolbox")
        self.assertEqual(scheduler_args.repo, "ExampleOrg/example-codex")

    def test_empty_release_repo_environment_is_ignored(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_PERSONAL_SYNC_DEFAULT_REPO": "",
                "CODEX_PERSONAL_SYNC_BASE_REPO": " ",
            },
        ):
            parser = MODULE.build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["install"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["install-private"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["install-scheduler"])

        install_private_args = parser.parse_args(
            ["install-private", "--repo", "ExampleOrg/private-codex"]
        )
        scheduler_args = parser.parse_args(
            [
                "install-scheduler",
                "--repo",
                "ExampleOrg/private-codex",
                "--mode",
                "private",
            ]
        )

        self.assertEqual(install_private_args.base_repo, "Joey-Tools/codex-toolbox")
        self.assertEqual(scheduler_args.base_repo, "Joey-Tools/codex-toolbox")

    def test_base_release_repo_can_be_overridden_by_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_PERSONAL_SYNC_DEFAULT_REPO": "ExampleOrg/private-codex",
                "CODEX_PERSONAL_SYNC_BASE_REPO": "ExampleOrg/public-codex",
            },
        ):
            parser = MODULE.build_parser()

        install_private_args = parser.parse_args(["install-private"])
        scheduler_args = parser.parse_args(["install-scheduler", "--mode", "private"])

        self.assertEqual(install_private_args.base_repo, "ExampleOrg/public-codex")
        self.assertEqual(scheduler_args.base_repo, "ExampleOrg/public-codex")

    def test_public_package_uses_public_manifest_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            dist_dir = temp_dir / "dist"
            subprocess.run(
                [
                    sys.executable,
                    str(PACKAGE_SCRIPT_PATH),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--sha",
                    SHA1,
                    "--output-dir",
                    str(dist_dir),
                ],
                check=True,
                text=True,
                capture_output=True,
            )

            archive_path = dist_dir / f"personal-codex-{SHA1}.tar.gz"
            checksum_path = dist_dir / f"personal-codex-{SHA1}.sha256"
            MODULE.verify_checksum(archive_path, checksum_path)
            with tarfile.open(archive_path, "r:gz") as archive:
                member_names = archive.getnames()

            joined_names = "\n".join(member_names)
            self.assertIn(
                f"personal-codex-{SHA1}/personal_codex/sync-manifest.json",
                member_names,
            )
            self.assertIn(
                f"personal-codex-{SHA1}/scripts/codex_personal_sync.py",
                member_names,
            )
            self.assertIn(
                f"personal-codex-{SHA1}/personal_codex/AGENTS.md",
                member_names,
            )
            self.assertIn(
                f"personal-codex-{SHA1}/personal_codex/skills/submodule-linked-worktrees/SKILL.md",
                member_names,
            )
            self.assertNotIn("cisco-trackers-lookup", joined_names)
            self.assertNotIn("remote-host-context", joined_names)
            self.assertNotIn("automations/", joined_names)

            release_root = MODULE.safe_extract_archive(archive_path, temp_dir / "extract")
            entries = MODULE.validate_release_tree(release_root)
            self.assertEqual(len(entries), 5)

    def test_package_builder_rejects_nested_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            repo_root = temp_dir / "repo"
            source_root = repo_root / "personal_codex" / "skills" / "example"
            source_root.mkdir(parents=True)
            (source_root / "SKILL.md").write_text("---\nname: example\n---\n", encoding="utf-8")
            (source_root / "leak").symlink_to(Path.home())
            manifest_path = repo_root / "personal_codex" / "test-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "source": "personal_codex/skills/example",
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

            result = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGE_SCRIPT_PATH),
                    "--repo-root",
                    str(repo_root),
                    "--manifest",
                    "personal_codex/test-manifest.json",
                    "--sha",
                    SHA1,
                    "--output-dir",
                    str(temp_dir / "dist"),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nested symlink", result.stderr)

    def test_package_builder_rejects_generated_file_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            repo_root = temp_dir / "repo"
            source_root = repo_root / "personal_codex" / "skills" / "example"
            source_root.mkdir(parents=True)
            (source_root / "SKILL.md").write_text("---\nname: example\n---\n", encoding="utf-8")
            (source_root / "generated.pyc").symlink_to(Path.home())
            manifest_path = repo_root / "personal_codex" / "test-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "source": "personal_codex/skills/example",
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

            result = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGE_SCRIPT_PATH),
                    "--repo-root",
                    str(repo_root),
                    "--manifest",
                    "personal_codex/test-manifest.json",
                    "--sha",
                    SHA1,
                    "--output-dir",
                    str(temp_dir / "dist"),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nested symlink", result.stderr)

    def test_package_builder_rejects_top_level_generated_file_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            repo_root = temp_dir / "repo"
            source_root = repo_root / "personal_codex" / "skills" / "example"
            source_root.mkdir(parents=True)
            (source_root / "generated.pyc").write_bytes(b"generated")
            manifest_path = repo_root / "personal_codex" / "test-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "source": "personal_codex/skills/example/generated.pyc",
                                "target": "skills/example/generated.pyc",
                                "kind": "skill",
                            }
                        ],
                        "reference_only": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGE_SCRIPT_PATH),
                    "--repo-root",
                    str(repo_root),
                    "--manifest",
                    "personal_codex/test-manifest.json",
                    "--sha",
                    SHA1,
                    "--output-dir",
                    str(temp_dir / "dist"),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generated manifest source", result.stderr)

    def test_package_builder_filters_generated_files_without_dropping_real_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            repo_root = temp_dir / "repo"
            source_root = repo_root / "personal_codex" / "skills" / "example"
            cache_root = source_root / "__pycache__"
            real_pyc_dir = source_root / "assets" / "fixture.pyc"
            cache_root.mkdir(parents=True)
            real_pyc_dir.mkdir(parents=True)
            (source_root / "SKILL.md").write_text("---\nname: example\n---\n", encoding="utf-8")
            (source_root / ".DS_Store").write_text("generated\n", encoding="utf-8")
            (source_root / "generated.pyc").write_bytes(b"generated")
            (source_root / "assets" / "generated.pyo").write_bytes(b"generated")
            (cache_root / "session_retrospective.cpython-314.pyc").write_bytes(b"generated")
            (real_pyc_dir / "fixture.txt").write_text("keep\n", encoding="utf-8")
            manifest_path = repo_root / "personal_codex" / "test-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "source": "personal_codex/skills/example",
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
            dist_dir = temp_dir / "dist"

            subprocess.run(
                [
                    sys.executable,
                    str(PACKAGE_SCRIPT_PATH),
                    "--repo-root",
                    str(repo_root),
                    "--manifest",
                    "personal_codex/test-manifest.json",
                    "--sha",
                    SHA1,
                    "--output-dir",
                    str(dist_dir),
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            archive_path = dist_dir / f"personal-codex-{SHA1}.tar.gz"
            with tarfile.open(archive_path, "r:gz") as archive:
                member_names = archive.getnames()

        joined_names = "\n".join(member_names)
        self.assertIn(f"personal-codex-{SHA1}/personal_codex/skills/example/SKILL.md", member_names)
        self.assertIn(f"personal-codex-{SHA1}/personal_codex/skills/example/assets/fixture.pyc/fixture.txt", member_names)
        self.assertNotIn("__pycache__", joined_names)
        self.assertNotIn(".DS_Store", joined_names)
        self.assertNotIn("/generated.pyc", joined_names)
        self.assertNotIn("/generated.pyo", joined_names)
        self.assertNotIn(".cpython-314.pyc", joined_names)

    def test_install_private_downloads_public_base_and_overlay(self) -> None:
        public_release = self.root / "public-release"
        private_release = self.root / "private-release"
        home = self.root / "home" / ".codex"
        write_minimal_release(public_release, agent_text="public\n")
        write_private_skill_only_release(private_release)
        downloads: list[tuple[str, str | None]] = []

        def fake_download(repo: str, destination: Path, *, sha: str | None = None):
            downloads.append((repo, sha))
            if repo == "Joey-Tools/codex-private-workflows":
                return MODULE.DownloadedRelease(
                    repo=repo,
                    assets=MODULE.ReleaseAssets(
                        tag_name="personal-codex-20260520-120000-2222222",
                        sha=SHA2,
                        archive_name=f"personal-codex-{SHA2}.tar.gz",
                        checksum_name=f"personal-codex-{SHA2}.sha256",
                    ),
                    release_root=private_release,
                )
            if repo == "Joey-Tools/codex-toolbox":
                return MODULE.DownloadedRelease(
                    repo=repo,
                    assets=MODULE.ReleaseAssets(
                        tag_name="personal-codex-20260520-120000-1111111",
                        sha=SHA1,
                        archive_name=f"personal-codex-{SHA1}.tar.gz",
                        checksum_name=f"personal-codex-{SHA1}.sha256",
                    ),
                    release_root=public_release,
                )
            raise AssertionError(f"unexpected repo: {repo}")

        with mock.patch.object(MODULE, "download_and_extract_release", fake_download):
            self.run_quietly(
                MODULE.install_private_from_github,
                "Joey-Tools/codex-private-workflows",
                home,
                base_repo="Fallback/base",
                owner="private",
                dry_run=False,
            )

        self.assertEqual(
            downloads,
            [
                ("Joey-Tools/codex-private-workflows", None),
                ("Joey-Tools/codex-toolbox", None),
            ],
        )
        self.assertTrue((home / "bin" / "codex-personal-sync").is_symlink())
        self.assertTrue((home / "skills" / "private-skill").is_symlink())
        self.run_quietly(MODULE.verify_overlay, home, "private")

    def test_private_scheduler_invokes_private_install_entrypoint(self) -> None:
        home = self.root / "home" / ".codex"
        args = MODULE._scheduler_install_args(
            Path("/runner"),
            "Joey-Tools/codex-private-workflows",
            home,
            mode="private",
            base_repo="Joey-Tools/codex-toolbox",
            owner="private",
        )

        self.assertEqual(
            args,
            [
                "/runner",
                "install-private",
                "--repo",
                "Joey-Tools/codex-private-workflows",
                "--base-repo",
                "Joey-Tools/codex-toolbox",
                "--owner",
                "private",
                "--home",
                str(home),
            ],
        )

    def test_select_release_assets_matches_tarball_and_checksum(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA1,
            "assets": [
                {"name": f"personal-codex-{SHA1}.tar.gz"},
                {"name": f"personal-codex-{SHA1}.sha256"},
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.sha, SHA1)
        self.assertEqual(assets.archive_name, f"personal-codex-{SHA1}.tar.gz")
        self.assertEqual(assets.checksum_name, f"personal-codex-{SHA1}.sha256")

    def test_select_release_assets_rejects_missing_checksum(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "assets": [{"name": f"personal-codex-{SHA1}.tar.gz"}],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "missing checksum"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_multiple_tarballs(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "assets": [
                {"name": f"personal-codex-{SHA1}.tar.gz"},
                {"name": f"personal-codex-{SHA2}.tar.gz"},
                {"name": f"personal-codex-{SHA1}.sha256"},
                {"name": f"personal-codex-{SHA2}.sha256"},
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "multiple tarball"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_ignores_suffixed_asset_names(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "assets": [
                {"name": f"personal-codex-{SHA1}.tar.gz.sig"},
                {"name": f"personal-codex-{SHA1}.sha256.bak"},
                {"name": f"personal-codex-{SHA1}.tar.gz"},
                {"name": f"personal-codex-{SHA1}.sha256"},
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.archive_name, f"personal-codex-{SHA1}.tar.gz")
        self.assertEqual(assets.checksum_name, f"personal-codex-{SHA1}.sha256")

    def test_select_release_assets_rejects_tag_sha_mismatch(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-2222222",
            "assets": [
                {"name": f"personal-codex-{SHA1}.tar.gz"},
                {"name": f"personal-codex-{SHA1}.sha256"},
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "does not match tag suffix"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_target_commit_mismatch(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA2,
            "assets": [
                {"name": f"personal-codex-{SHA1}.tar.gz"},
                {"name": f"personal-codex-{SHA1}.sha256"},
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "does not match target commit"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_accepts_github_api_payload(self) -> None:
        release = {
            "tag_name": "personal-codex-20260511-120000-1111111",
            "target_commitish": SHA1,
            "assets": [
                {"name": f"personal-codex-{SHA1}.tar.gz"},
                {"name": f"personal-codex-{SHA1}.sha256"},
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.sha, SHA1)

    def test_run_gh_json_wraps_missing_gh(self) -> None:
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=FileNotFoundError("No such file or directory"),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "GitHub CLI `gh` is not available"):
                MODULE._run_gh_json(["api", "repos/owner/repo/releases"])

    def test_run_gh_wraps_missing_gh(self) -> None:
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=FileNotFoundError("No such file or directory"),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "GitHub CLI `gh` is not available"):
                MODULE._run_gh(["release", "download", "tag"])

    def test_run_gh_json_stream_accepts_concatenated_pages(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='[{"tag_name": "one"}]\n[{"tag_name": "two"}]\n',
            stderr="",
        )

        with mock.patch.object(MODULE, "_run_gh_process", return_value=completed):
            pages = MODULE._run_gh_json_stream(["api", "repos/owner/repo/releases"])

        self.assertEqual(pages, [[{"tag_name": "one"}], [{"tag_name": "two"}]])

    def test_download_release_assets_uses_old_gh_compatible_flags(self) -> None:
        calls: list[list[str]] = []
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
        )

        def fake_run_gh(args):
            calls.append(args)

        with mock.patch.object(MODULE, "_run_gh", fake_run_gh):
            MODULE.download_release_assets("owner/repo", assets, self.root / "downloads")

        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call[:3], ["release", "download", assets.tag_name])
            self.assertIn("--repo", call)
            self.assertIn("--pattern", call)
            self.assertIn("--dir", call)
            self.assertNotIn("--clobber", call)

    def test_verify_checksum_rejects_mismatch(self) -> None:
        archive = self.root / f"personal-codex-{SHA1}.tar.gz"
        checksum = self.root / f"personal-codex-{SHA1}.sha256"
        archive.write_bytes(b"payload")
        checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")

        with self.assertRaisesRegex(MODULE.SyncError, "checksum mismatch"):
            MODULE.verify_checksum(archive, checksum)

    def test_verify_checksum_accepts_matching_file(self) -> None:
        archive = self.root / f"personal-codex-{SHA1}.tar.gz"
        checksum = self.root / f"personal-codex-{SHA1}.sha256"
        archive.write_bytes(b"payload")
        digest = hashlib.sha256(b"payload").hexdigest()
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")

        MODULE.verify_checksum(archive, checksum)

    def test_verify_checksum_accepts_binary_mode_file(self) -> None:
        archive = self.root / f"personal-codex-{SHA1}.tar.gz"
        checksum = self.root / f"personal-codex-{SHA1}.sha256"
        archive.write_bytes(b"payload")
        digest = hashlib.sha256(b"payload").hexdigest()
        checksum.write_text(f"{digest} *{archive.name}\n", encoding="utf-8")

        MODULE.verify_checksum(archive, checksum)

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        archive_path = self.root / "unsafe.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            data = b"bad"
            member = tarfile.TarInfo("../evil.txt")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

        with self.assertRaisesRegex(MODULE.SyncError, "unsafe archive member path"):
            MODULE.safe_extract_archive(archive_path, self.root / "extract")
        self.assertFalse((self.root / "evil.txt").exists())

    def test_safe_extract_rejects_hardlink_member(self) -> None:
        archive_path = self.root / "hardlink.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("personal-codex/link")
            member.type = tarfile.LNKTYPE
            member.linkname = "target"
            archive.addfile(member)

        with self.assertRaisesRegex(MODULE.SyncError, "archive link member"):
            MODULE.safe_extract_archive(archive_path, self.root / "extract")

    def test_safe_extract_fallback_sanitizes_member_modes(self) -> None:
        source_root = self.root / "source"
        write_minimal_release(source_root)
        executable = source_root / "personal_codex" / "bin" / "example-tool"
        executable.chmod(0o6777)
        archive_path = self.root / "release.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        original_extractall = tarfile.TarFile.extractall

        def fallback_extractall(self, path=".", members=None, *, numeric_owner=False, filter=None):
            if filter is not None:
                raise TypeError("filter is unavailable")
            return original_extractall(
                self,
                path,
                members=members,
                numeric_owner=numeric_owner,
            )

        with (
            mock.patch.object(tarfile.TarFile, "extractall", fallback_extractall),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore", DeprecationWarning)
            release_root = MODULE.safe_extract_archive(archive_path, self.root / "extract")

        mode = (release_root / "personal_codex" / "bin" / "example-tool").stat().st_mode
        self.assertEqual(mode & 0o7000, 0)
        self.assertEqual(mode & 0o022, 0)
        self.assertTrue(mode & 0o100)

    def test_load_manifest_requires_skill_markdown(self) -> None:
        release_root = self.root / "release"
        write_minimal_release(release_root)
        (release_root / "personal_codex" / "skills" / "example-skill" / "SKILL.md").unlink()

        with self.assertRaisesRegex(MODULE.SyncError, "missing SKILL.md"):
            MODULE.load_manifest(release_root)

    def test_load_manifest_rejects_parent_traversal(self) -> None:
        release_root = self.root / "release"
        write_minimal_release(release_root)
        manifest = release_root / "personal_codex" / "sync-manifest.json"
        manifest.write_text(
            """
{
  "version": 1,
  "links": [
    {
      "source": "../AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    }
  ],
  "reference_only": []
}
""".strip()
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MODULE.SyncError, "parent traversal"):
            MODULE.load_manifest(release_root)

    def test_load_manifest_rejects_duplicate_targets(self) -> None:
        release_root = self.root / "release"
        write_minimal_release(release_root)
        manifest = release_root / "personal_codex" / "sync-manifest.json"
        manifest.write_text(
            """
{
  "version": 1,
  "links": [
    {
      "source": "personal_codex/AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    },
    {
      "source": "personal_codex/AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    }
  ],
  "reference_only": []
}
""".strip()
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MODULE.SyncError, "duplicate manifest target"):
            MODULE.load_manifest(release_root)

    def test_load_manifest_rejects_missing_reference_only_path(self) -> None:
        release_root = self.root / "release"
        write_minimal_release(release_root)
        manifest = release_root / "personal_codex" / "sync-manifest.json"
        manifest.write_text(
            """
{
  "version": 1,
  "links": [
    {
      "source": "personal_codex/AGENTS.md",
      "target": "AGENTS.md",
      "kind": "file"
    }
  ],
  "reference_only": [
    "personal_codex/automations/missing/automation.toml"
  ]
}
""".strip()
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MODULE.SyncError, "reference_only path is missing"):
            MODULE.load_manifest(release_root)

    def test_find_release_root_rejects_multiple_candidates(self) -> None:
        extract_root = self.root / "extract"
        write_minimal_release(extract_root / "one")
        write_minimal_release(extract_root / "two")

        with self.assertRaisesRegex(MODULE.SyncError, "exactly one release root"):
            MODULE.find_release_root(extract_root)

    def test_install_release_tree_creates_current_and_symlink_farm(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        (home / "skills" / ".system").mkdir(parents=True)
        (home / "skills" / "host-local").mkdir()

        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "AGENTS.md").is_symlink())
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "agent\n")
        self.assertTrue((home / "skills" / "example-skill").is_symlink())
        self.assertTrue((home / "bin" / "example-tool").is_symlink())
        self.assertTrue((home / "bin" / "codex-personal-sync").is_symlink())
        self.assertTrue((home / "skills" / ".system").is_dir())
        self.assertTrue((home / "skills" / "host-local").is_dir())

    def test_install_release_tree_rejects_non_symlink_current_pointer(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        (home / "personal-sync" / "current").mkdir(parents=True)

        with self.assertRaisesRegex(MODULE.SyncError, "non-symlink current pointer"):
            self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)

    def test_install_release_tree_recovers_when_release_dir_already_exists(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        release_dir = home / "personal-sync" / "releases" / SHA1
        release_dir.parent.mkdir(parents=True)
        shutil.copytree(release_root, release_dir)

        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "AGENTS.md").is_symlink())
        self.assertTrue((home / "bin" / "codex-personal-sync").is_symlink())

    def test_install_release_tree_is_idempotent_for_current_release(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)

        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "agent\n")

    def test_install_release_tree_removes_stale_links_after_manifest_shrink(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        (home / "skills" / ".system").mkdir(parents=True)
        (home / "skills" / "host-local").mkdir()
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)

        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA2}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "two\n")
        self.assertFalse(os.path.lexists(home / "skills" / "example-skill"))
        self.assertFalse(os.path.lexists(home / "bin" / "example-tool"))
        self.assertFalse(os.path.lexists(home / "bin" / "codex-personal-sync"))
        self.assertTrue((home / "skills" / ".system").is_dir())
        self.assertTrue((home / "skills" / "host-local").is_dir())

    def test_install_release_tree_repairs_stale_links_after_interrupted_switch(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        release_two_dir = home / "personal-sync" / "releases" / SHA2
        shutil.copytree(release_two, release_two_dir)
        self.run_quietly(MODULE._switch_current, home, SHA2, dry_run=False)

        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA2}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "two\n")
        self.assertFalse(os.path.lexists(home / "skills" / "example-skill"))
        self.assertFalse(os.path.lexists(home / "bin" / "example-tool"))
        self.assertFalse(os.path.lexists(home / "bin" / "codex-personal-sync"))

    def test_install_release_tree_preserves_existing_local_agents_file(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root, agent_text="public\n")
        home.mkdir(parents=True)
        (home / "AGENTS.md").write_text("local\n", encoding="utf-8")

        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertFalse((home / "AGENTS.md").is_symlink())
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "local\n")
        self.assertTrue((home / "bin" / "example-tool").is_symlink())

    def test_install_release_tree_preserves_existing_local_agents_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        dotfiles = self.root / "dotfiles"
        write_minimal_release(release_root, agent_text="public\n")
        home.mkdir(parents=True)
        dotfiles.mkdir()
        local_agents = dotfiles / "AGENTS.md"
        local_agents.write_text("local\n", encoding="utf-8")
        (home / "AGENTS.md").symlink_to(local_agents)

        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").readlink(), local_agents)
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "local\n")
        self.assertTrue((home / "bin" / "example-tool").is_symlink())

    def test_install_release_tree_rejects_existing_non_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        target = home / "bin" / "example-tool"
        target.parent.mkdir(parents=True)
        target.write_text("local\n", encoding="utf-8")

        with self.assertRaisesRegex(MODULE.SyncError, "non-symlink target"):
            self.run_quietly(
                MODULE.install_release_tree,
                release_root,
                home,
                SHA1,
                dry_run=False,
            )
        self.assertFalse((home / "personal-sync").exists())

    def test_install_private_keeps_legacy_agent_overlay_without_override(self) -> None:
        public_release = self.root / "public-release"
        private_release = self.root / "private-release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(public_release, agent_text="public\n")
        write_private_agent_release(private_release, agent_text="private\n")

        def fake_download(repo: str, destination: Path, *, sha: str | None = None):
            if repo == "Joey-Tools/codex-private-workflows":
                return MODULE.DownloadedRelease(
                    repo=repo,
                    assets=MODULE.ReleaseAssets(
                        tag_name="personal-codex-20260520-120000-2222222",
                        sha=SHA2,
                        archive_name=f"personal-codex-{SHA2}.tar.gz",
                        checksum_name=f"personal-codex-{SHA2}.sha256",
                    ),
                    release_root=private_release,
                )
            if repo == "Joey-Tools/codex-toolbox":
                return MODULE.DownloadedRelease(
                    repo=repo,
                    assets=MODULE.ReleaseAssets(
                        tag_name="personal-codex-20260520-120000-1111111",
                        sha=SHA1,
                        archive_name=f"personal-codex-{SHA1}.tar.gz",
                        checksum_name=f"personal-codex-{SHA1}.sha256",
                    ),
                    release_root=public_release,
                )
            raise AssertionError(f"unexpected repo: {repo}")

        with mock.patch.object(MODULE, "download_and_extract_release", fake_download):
            self.run_quietly(
                MODULE.install_private_from_github,
                "Joey-Tools/codex-private-workflows",
                home,
                base_repo="Fallback/base",
                owner="private",
                dry_run=False,
            )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(
            (home / "personal-sync" / "overlays" / "private" / "current").readlink().as_posix(),
            f"releases/{SHA2}",
        )
        self.assertTrue((home / "AGENTS.md").is_symlink())
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "private\n")
        self.run_quietly(MODULE.verify_overlay, home, "private")

    def test_dry_run_does_not_mutate_home(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)

        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=True)

        self.assertFalse(home.exists())

    def test_private_rollback_is_rejected(self) -> None:
        home = self.root / "home" / ".codex"

        with self.assertRaisesRegex(MODULE.SyncError, "only public releases"):
            self.run_quietly(MODULE.rollback, home, None, "private")

    def test_rollback_switches_to_requested_release(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "one\n")

    def test_rollback_removes_stale_links_after_manifest_shrink(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "one\n")
        self.assertFalse(os.path.lexists(home / "skills" / "example-skill"))
        self.assertFalse(os.path.lexists(home / "bin" / "example-tool"))
        self.assertFalse(os.path.lexists(home / "bin" / "codex-personal-sync"))

    def test_rollback_repairs_stale_links_after_interrupted_switch(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)
        self.run_quietly(MODULE._switch_current, home, SHA1, dry_run=False)

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "one\n")
        self.assertFalse(os.path.lexists(home / "skills" / "example-skill"))
        self.assertFalse(os.path.lexists(home / "bin" / "example-tool"))
        self.assertFalse(os.path.lexists(home / "bin" / "codex-personal-sync"))

    def test_rollback_without_target_uses_most_recent_non_current_release(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)

        self.run_quietly(MODULE.rollback, home, None)

        self.assertEqual(current_target(home), f"releases/{SHA1}")

    def test_rollback_without_target_uses_release_directory_mtime_order(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        release_three = self.root / "release-three"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        write_minimal_release(release_three, agent_text="three\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_three, home, SHA3, dry_run=False)
        os.utime(home / "personal-sync" / "releases" / SHA1, (300, 300))
        os.utime(home / "personal-sync" / "releases" / SHA2, (200, 200))
        os.utime(home / "personal-sync" / "releases" / SHA3, (100, 100))

        self.run_quietly(MODULE.rollback, home, None)

        self.assertEqual(current_target(home), f"releases/{SHA1}")

    def test_rollback_without_target_ignores_incomplete_release_directories(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)
        releases_root = home / "personal-sync" / "releases"
        (releases_root / f".tmp-{SHA3}-123").mkdir()
        (releases_root / SHA3).mkdir()
        os.utime(releases_root / f".tmp-{SHA3}-123", (500, 500))
        os.utime(releases_root / SHA3, (400, 400))
        os.utime(releases_root / SHA1, (300, 300))
        os.utime(releases_root / SHA2, (200, 200))

        self.run_quietly(MODULE.rollback, home, None)

        self.assertEqual(current_target(home), f"releases/{SHA1}")

    def test_rollback_to_target_ignores_invalid_release_directory(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        (home / "personal-sync" / "releases" / SHA3).mkdir()

        with self.assertRaisesRegex(MODULE.SyncError, f"no release matches {SHA3[:8]}"):
            self.run_quietly(MODULE.rollback, home, SHA3[:8])

    def test_rollback_to_current_release_repairs_symlink_drift(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        (home / "AGENTS.md").unlink()

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "AGENTS.md").is_symlink())
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "agent\n")

    def test_rollback_to_current_release_preserves_unmanaged_current_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        unmanaged_link = home / "bin" / "local-tool"
        unmanaged_link.parent.mkdir(parents=True, exist_ok=True)
        unmanaged_link.symlink_to("../personal-sync/current/personal_codex/bin/local-tool")

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertTrue(unmanaged_link.is_symlink())

    def test_rollback_to_current_release_ignores_incomplete_tmp_manifest_targets(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        tmp_release = home / "personal-sync" / "releases" / f".tmp-{SHA2}-123"
        write_minimal_release(tmp_release)
        unmanaged_link = home / "bin" / "example-tool"
        unmanaged_link.parent.mkdir(parents=True, exist_ok=True)
        unmanaged_link.symlink_to("../personal-sync/current/personal_codex/bin/example-tool")

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertTrue(unmanaged_link.is_symlink())

    def test_rollback_to_current_release_preserves_known_target_with_unmanaged_link(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)
        unmanaged_link = home / "bin" / "example-tool"
        unmanaged_link.symlink_to("../personal-sync/current/personal_codex/bin/local-tool")

        self.run_quietly(MODULE.rollback, home, SHA2[:8])

        self.assertTrue(unmanaged_link.is_symlink())
        self.assertEqual(
            unmanaged_link.readlink().as_posix(),
            "../personal-sync/current/personal_codex/bin/local-tool",
        )

    def test_status_reports_not_installed(self) -> None:
        home = self.root / "home" / ".codex"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            MODULE.status(home)

        self.assertIn("not installed", output.getvalue())

    def test_status_ignores_unmanaged_current_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        stale_target = home / "skills" / "stale-skill"
        stale_target.parent.mkdir(parents=True, exist_ok=True)
        stale_target.symlink_to(
            "../personal-sync/current/personal_codex/skills/stale-skill",
            target_is_directory=True,
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            MODULE.status(home)

        status_output = output.getvalue()
        self.assertIn("current manifest symlinks: ok", status_output)
        self.assertNotIn("stale managed symlinks", status_output)
        self.assertNotIn(str(stale_target), status_output)

    def test_status_ignores_near_miss_current_symlink_substring(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        near_miss = home / "skills" / "near-miss"
        near_miss.parent.mkdir(parents=True, exist_ok=True)
        near_miss.symlink_to(
            "../other/personal-sync/current/personal_codex/skills/near-miss",
            target_is_directory=True,
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            MODULE.status(home)

        self.assertNotIn("stale managed symlinks", output.getvalue())

    def test_status_reports_broken_current_pointer(self) -> None:
        home = self.root / "home" / ".codex"
        current = home / "personal-sync" / "current"
        current.parent.mkdir(parents=True)
        current.symlink_to(Path("releases") / SHA1, target_is_directory=True)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            MODULE.status(home)

        self.assertIn("current pointer is broken", output.getvalue())

    def test_status_reports_stale_symlink_from_historical_manifest_root(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_rules_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)
        stale_target = home / "rules" / "example-rule"
        stale_target.symlink_to(
            "../personal-sync/current/personal_codex/rules/example-rule"
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            MODULE.status(home)

        status_output = output.getvalue()
        self.assertIn("stale managed symlinks: 1", status_output)
        self.assertIn(str(stale_target), status_output)

    def test_find_latest_release_uses_paginated_api(self) -> None:
        calls: list[list[str]] = []

        def fake_run_gh_json(args):
            calls.append(args)
            return [
                [
                    {
                        "tag_name": "personal-codex-20260511-120000-1111111",
                        "target_commitish": SHA1,
                        "assets": [
                            {"name": f"personal-codex-{SHA1}.tar.gz"},
                            {"name": f"personal-codex-{SHA1}.sha256"},
                        ],
                    }
                ]
            ]

        with mock.patch.object(MODULE, "_run_gh_json_stream", fake_run_gh_json):
            release = MODULE.find_latest_release("owner/repo")

        self.assertEqual(release["tagName"], "personal-codex-20260511-120000-1111111")
        self.assertEqual(calls[0][0], "api")
        self.assertIn("--paginate", calls[0])
        self.assertNotIn("--slurp", calls[0])
        self.assertNotIn("--jq", calls[0])

    def test_find_latest_release_rejects_missing_release(self) -> None:
        with mock.patch.object(MODULE, "_run_gh_json_stream", return_value=[[]]):
            with self.assertRaisesRegex(MODULE.SyncError, "no personal-codex- release"):
                MODULE.find_latest_release("owner/repo")

    def test_current_sha_accepts_absolute_current_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        current = home / "personal-sync" / "current"
        current.unlink()
        current.symlink_to(home / "personal-sync" / "releases" / SHA1, target_is_directory=True)

        self.assertEqual(MODULE._current_sha(home), SHA1)

    def test_install_from_github_downloads_verifies_extracts_and_installs(self) -> None:
        source_root = self.root / "source"
        home = self.root / "home" / ".codex"
        write_minimal_release(source_root)
        archive_name = f"personal-codex-{SHA1}.tar.gz"
        checksum_name = f"personal-codex-{SHA1}.sha256"
        archive_path = self.root / archive_name
        checksum_path = self.root / checksum_name
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        checksum_path.write_text(f"{digest}  {archive_name}\n", encoding="utf-8")
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA1,
            "assets": [
                {"name": archive_name},
                {"name": checksum_name},
            ],
        }

        def fake_download(repo, assets, destination):
            self.assertEqual(repo, "owner/repo")
            self.assertEqual(assets.archive_name, archive_name)
            shutil.copy2(archive_path, destination / archive_name)
            shutil.copy2(checksum_path, destination / checksum_name)

        with (
            mock.patch.object(MODULE, "find_latest_release", return_value=release),
            mock.patch.object(MODULE, "download_release_assets", fake_download),
        ):
            self.run_quietly(MODULE.install_from_github, "owner/repo", home, dry_run=False)

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "AGENTS.md").is_symlink())

    def test_install_from_github_rejects_downloaded_checksum_mismatch(self) -> None:
        source_root = self.root / "source"
        home = self.root / "home" / ".codex"
        write_minimal_release(source_root)
        archive_name = f"personal-codex-{SHA1}.tar.gz"
        checksum_name = f"personal-codex-{SHA1}.sha256"
        archive_path = self.root / archive_name
        checksum_path = self.root / checksum_name
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        checksum_path.write_text(f"{'0' * 64}  {archive_name}\n", encoding="utf-8")
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA1,
            "assets": [
                {"name": archive_name},
                {"name": checksum_name},
            ],
        }

        def fake_download(repo, assets, destination):
            shutil.copy2(archive_path, destination / archive_name)
            shutil.copy2(checksum_path, destination / checksum_name)

        with (
            mock.patch.object(MODULE, "find_latest_release", return_value=release),
            mock.patch.object(MODULE, "download_release_assets", fake_download),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "checksum mismatch"):
                self.run_quietly(MODULE.install_from_github, "owner/repo", home, dry_run=False)

        self.assertFalse(home.exists())

    def test_install_scheduler_requires_existing_runner(self) -> None:
        home = self.root / "home" / ".codex"

        with self.assertRaisesRegex(MODULE.SyncError, "scheduler runner is missing"):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

    def test_install_scheduler_rejects_invalid_interval(self) -> None:
        home = self.root / "home" / ".codex"

        with self.assertRaisesRegex(MODULE.SyncError, "at least 1 minute"):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                0,
                "linux",
                str(self.root / "runner"),
                dry_run=True,
                enable=False,
            )

    def test_install_scheduler_requires_current_user_codex_home(self) -> None:
        home = self.root / "home" / "codex-alt"
        write_scheduler_runner(home)

        with self.assertRaisesRegex(MODULE.SyncError, "current user's ~/.codex"):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

        other_home = self.root / "other" / ".codex"
        write_scheduler_runner(other_home)
        with self.assertRaisesRegex(MODULE.SyncError, "current user's ~/.codex"):
            MODULE.install_scheduler(
                other_home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

    def test_install_scheduler_writes_macos_launchd_plist(self) -> None:
        home = self.root / "home" / ".codex"
        runner = write_scheduler_runner(home)

        self.run_quietly(
            MODULE.install_scheduler,
            home,
            "owner/repo",
            30,
            "macos",
            None,
            dry_run=False,
            enable=False,
        )

        plist_path = (
            self.root
            / "home"
            / "Library"
            / "LaunchAgents"
            / f"{MODULE.LAUNCHD_LABEL}.plist"
        )
        with plist_path.open("rb") as file:
            payload = plistlib.load(file)
        self.assertEqual(payload["Label"], MODULE.LAUNCHD_LABEL)
        self.assertEqual(payload["StartInterval"], 1800)
        self.assertEqual(
            payload["ProgramArguments"],
            [
                str(runner),
                "install",
                "--repo",
                "owner/repo",
                "--home",
                str(home),
            ],
        )
        self.assertEqual(
            payload["EnvironmentVariables"]["PATH"],
            MODULE.MACOS_SCHEDULER_PATH,
        )
        self.assertIn("codex-personal-sync.out.log", payload["StandardOutPath"])

    def test_install_scheduler_no_enable_keeps_legacy_macos_plist(self) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        legacy_plist = (
            self.root
            / "home"
            / "Library"
            / "LaunchAgents"
            / f"{MODULE.LEGACY_LAUNCHD_LABELS[0]}.plist"
        )
        legacy_plist.parent.mkdir(parents=True)
        legacy_plist.write_text("legacy\n", encoding="utf-8")

        self.run_quietly(
            MODULE.install_scheduler,
            home,
            "owner/repo",
            60,
            "macos",
            None,
            dry_run=False,
            enable=False,
        )

        self.assertTrue(legacy_plist.exists())

    def test_install_scheduler_runs_macos_enable_commands(self) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.run_quietly(
                MODULE.install_scheduler,
                home,
                "owner/repo",
                60,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        calls = [call.args[0] for call in run.call_args_list]
        plist_path = (
            self.root
            / "home"
            / "Library"
            / "LaunchAgents"
            / f"{MODULE.LAUNCHD_LABEL}.plist"
        )
        domain = f"gui/{os.getuid()}"
        self.assertIn(["launchctl", "bootout", domain, str(plist_path)], calls)
        self.assertIn(["launchctl", "bootstrap", domain, str(plist_path)], calls)
        self.assertIn(["launchctl", "enable", f"{domain}/{MODULE.LAUNCHD_LABEL}"], calls)
        self.assertFalse(any(call[:2] == ["launchctl", "kickstart"] for call in calls))

    def test_install_scheduler_writes_linux_systemd_units(self) -> None:
        home = self.root / "home" / ".codex"
        runner = write_scheduler_runner(home)

        self.run_quietly(
            MODULE.install_scheduler,
            home,
            "owner/repo",
            45,
            "linux",
            None,
            dry_run=False,
            enable=False,
        )

        unit_root = self.root / "home" / ".config" / "systemd" / "user"
        service = (unit_root / "codex-personal-sync.service").read_text(encoding="utf-8")
        timer = (unit_root / "codex-personal-sync.timer").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn(f'Environment="PATH={MODULE.LINUX_SCHEDULER_PATH}"', service)
        self.assertIn(f'ExecStart="{runner}" "install"', service)
        self.assertIn('"--repo" "owner/repo"', service)
        self.assertIn(f'"--home" "{home}"', service)
        self.assertIn("OnBootSec=5min", timer)
        self.assertIn("OnUnitActiveSec=45min", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_install_scheduler_runs_linux_enable_commands(self) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.run_quietly(
                MODULE.install_scheduler,
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(["systemctl", "--user", "daemon-reload"], calls)
        self.assertIn(
            ["systemctl", "--user", "enable", "--now", "codex-personal-sync.timer"],
            calls,
        )

    def test_uninstall_scheduler_removes_linux_units(self) -> None:
        home = self.root / "home" / ".codex"
        unit_root = self.root / "home" / ".config" / "systemd" / "user"
        unit_root.mkdir(parents=True)
        service = unit_root / "codex-personal-sync.service"
        timer = unit_root / "codex-personal-sync.timer"
        service.write_text("service\n", encoding="utf-8")
        timer.write_text("timer\n", encoding="utf-8")

        self.run_quietly(
            MODULE.uninstall_scheduler,
            home,
            "linux",
            dry_run=False,
            disable=False,
        )

        self.assertFalse(service.exists())
        self.assertFalse(timer.exists())

    def test_uninstall_scheduler_removes_macos_plist(self) -> None:
        home = self.root / "home" / ".codex"
        plist_path = (
            self.root
            / "home"
            / "Library"
            / "LaunchAgents"
            / f"{MODULE.LAUNCHD_LABEL}.plist"
        )
        plist_path.parent.mkdir(parents=True)
        plist_path.write_text("plist\n", encoding="utf-8")

        self.run_quietly(
            MODULE.uninstall_scheduler,
            home,
            "macos",
            dry_run=False,
            disable=False,
        )

        self.assertFalse(plist_path.exists())

    def test_uninstall_scheduler_no_disable_removes_legacy_macos_plist(self) -> None:
        home = self.root / "home" / ".codex"
        legacy_plist = (
            self.root
            / "home"
            / "Library"
            / "LaunchAgents"
            / f"{MODULE.LEGACY_LAUNCHD_LABELS[0]}.plist"
        )
        legacy_plist.parent.mkdir(parents=True)
        legacy_plist.write_text("legacy\n", encoding="utf-8")

        self.run_quietly(
            MODULE.uninstall_scheduler,
            home,
            "macos",
            dry_run=False,
            disable=False,
        )

        self.assertFalse(legacy_plist.exists())

    def test_uninstall_scheduler_runs_macos_disable_commands(self) -> None:
        home = self.root / "home" / ".codex"
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.run_quietly(
                MODULE.uninstall_scheduler,
                home,
                "macos",
                dry_run=False,
                disable=True,
            )

        plist_path = (
            self.root
            / "home"
            / "Library"
            / "LaunchAgents"
            / f"{MODULE.LAUNCHD_LABEL}.plist"
        )
        domain = f"gui/{os.getuid()}"
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(["launchctl", "bootout", domain, str(plist_path)], calls)
        self.assertIn(["launchctl", "disable", f"{domain}/{MODULE.LAUNCHD_LABEL}"], calls)


if __name__ == "__main__":
    unittest.main()
