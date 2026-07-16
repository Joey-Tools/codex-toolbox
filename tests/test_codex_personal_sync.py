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
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
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
SHA4 = "4" * 40
SHA5 = "5" * 40
SHA6 = "6" * 40


class FakeDownloadProcess:
    def __init__(self, payload: bytes, *, returncode: int = 0) -> None:
        self.stdout = io.BytesIO(payload)
        self.final_returncode = returncode
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def snapshot_tree(root: Path) -> tuple[tuple[str, str, int, bytes | str | None], ...]:
    if not os.path.lexists(root):
        return ()
    entries: list[tuple[str, str, int, bytes | str | None]] = []

    def visit(path: Path) -> None:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            entries.append((relative, "symlink", mode, os.readlink(path)))
            return
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode, None))
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        if stat.S_ISREG(metadata.st_mode):
            entries.append((relative, "file", mode, path.read_bytes()))
            return
        entries.append((relative, "other", mode, None))

    visit(root)
    return tuple(entries)


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


def write_skill_manifest_release(
    release_root: Path,
    *,
    owner: str = "public",
    skills: tuple[str, ...] = ("example-skill",),
    removed_links: list[dict[str, object]] | None = None,
    extra_skill_dirs: tuple[str, ...] = (),
) -> None:
    personal_root = release_root / "personal_codex"
    skills_root = personal_root / "skills"
    skills_root.mkdir(parents=True)
    links: list[dict[str, object]] = []
    for skill in (*skills, *extra_skill_dirs):
        skill_root = skills_root / skill
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {skill}\n---\n",
            encoding="utf-8",
        )
    for skill in skills:
        links.append(
            {
                "source": f"personal_codex/skills/{skill}",
                "target": f"skills/{skill}",
                "kind": "skill",
            }
        )
    manifest: dict[str, object] = {
        "version": 1,
        "links": links,
        "reference_only": [],
    }
    if owner != "public":
        manifest["owner"] = owner
        manifest["base_release"] = {"repo": "Joey-Tools/codex-toolbox"}
    if removed_links is not None:
        manifest["removed_links"] = removed_links
    (personal_root / "sync-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
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


class PersonalGuidelinesContentTests(unittest.TestCase):
    def test_agents_guidance_requires_bounded_command_output(self) -> None:
        agents = (REPO_ROOT / "personal_codex" / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("large or unbounded output", agents)
        self.assertIn("narrow its inputs or results", agents)
        self.assertIn("capture complete output in a task-scoped file", agents)
        self.assertIn("counts, candidate filenames, decisive key lines, or a short tail", agents)
        self.assertIn("backstops, not execution-time bounds", agents)
        self.assertNotIn("$bounded-command-output", agents)


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

    @contextlib.contextmanager
    def capture_reconcile_backups(self):
        events: list[tuple[str, str, str | None, str | None]] = []
        real_verify = MODULE._verify_reconcile_backup

        def capture(home: Path, action, backup: Path) -> None:
            real_verify(home, action, backup)
            relative_target = action.target.relative_to(home)
            relative_backup = backup.relative_to(
                home / "personal-sync" / "quarantine"
            )
            self.assertGreaterEqual(len(relative_backup.parts), 3)
            self.assertIsNotNone(
                MODULE.PENDING_LINK_BATCH_RE.fullmatch(relative_backup.parts[0])
            )
            self.assertEqual(
                relative_backup.parts[1:],
                ("links", *relative_target.parts),
            )
            self.assertFalse(os.path.lexists(action.target))
            events.append(
                (
                    action.action,
                    relative_target.as_posix(),
                    action.expected_link_target,
                    action.removed_link_key,
                )
            )

        with mock.patch.object(
            MODULE,
            "_verify_reconcile_backup",
            side_effect=capture,
        ):
            yield events

    def install_private_pair(
        self,
        home: Path,
        public_release: Path,
        private_release: Path,
        *,
        public_sha: str,
        private_sha: str,
        dry_run: bool = False,
    ) -> None:
        def fake_download(repo: str, destination: Path, *, sha: str | None = None):
            if repo == "Joey-Tools/codex-private-workflows":
                return MODULE.DownloadedRelease(
                    repo=repo,
                    assets=MODULE.ReleaseAssets(
                        tag_name=f"personal-codex-20260520-120000-{private_sha[:7]}",
                        sha=private_sha,
                        archive_name=f"personal-codex-{private_sha}.tar.gz",
                        checksum_name=f"personal-codex-{private_sha}.sha256",
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
                    ),
                    release_root=private_release,
                )
            if repo == "Joey-Tools/codex-toolbox":
                return MODULE.DownloadedRelease(
                    repo=repo,
                    assets=MODULE.ReleaseAssets(
                        tag_name=f"personal-codex-20260520-120000-{public_sha[:7]}",
                        sha=public_sha,
                        archive_name=f"personal-codex-{public_sha}.tar.gz",
                        checksum_name=f"personal-codex-{public_sha}.sha256",
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
                    ),
                    release_root=public_release,
                )
            raise AssertionError(f"unexpected repo: {repo}")

        with mock.patch.object(MODULE, "download_and_extract_release", fake_download):
            self.run_quietly(
                MODULE.install_private_from_github,
                "Joey-Tools/codex-private-workflows",
                home,
                base_repo="Joey-Tools/codex-toolbox",
                owner="private",
                dry_run=dry_run,
            )

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

    def test_package_builder_rejects_current_directory_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            repo_root = temp_dir / "repo"
            manifest_path = repo_root / "personal_codex" / "test-manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "source": ".",
                                "target": "skills/example",
                                "kind": "directory",
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
        self.assertIn("unsafe manifest source", result.stderr)

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
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
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
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
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

    def test_install_private_uses_validated_base_release_spec(self) -> None:
        public_release = self.root / "public-release"
        private_release = self.root / "private-release"
        home = self.root / "home" / ".codex"
        write_minimal_release(public_release, agent_text="public\n")
        write_private_skill_only_release(private_release)
        manifest_path = private_release / "personal_codex" / "sync-manifest.json"
        original_manifest = manifest_path.read_bytes()
        downloads: list[tuple[str, str | None]] = []
        overlay_validated = False
        real_validate = MODULE._validate_release_manifest_owner

        def validate_then_replace_manifest(
            release_root: Path,
            expected_owner: str,
            release_expectation: MODULE.ReleaseTreeExpectation | None = None,
        ):
            nonlocal overlay_validated
            manifest = real_validate(
                release_root,
                expected_owner,
                release_expectation,
            )
            if expected_owner == "private":
                payload = json.loads(original_manifest.decode("utf-8"))
                payload["base_release"] = {"repo": "Attacker/alternate-base"}
                manifest_path.write_text(
                    json.dumps(payload) + "\n",
                    encoding="utf-8",
                )
                overlay_validated = True
            return manifest

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
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
                    ),
                    release_root=private_release,
                )
            if overlay_validated:
                manifest_path.write_bytes(original_manifest)
            if repo == "Joey-Tools/codex-toolbox":
                return MODULE.DownloadedRelease(
                    repo=repo,
                    assets=MODULE.ReleaseAssets(
                        tag_name="personal-codex-20260520-120000-1111111",
                        sha=SHA1,
                        archive_name=f"personal-codex-{SHA1}.tar.gz",
                        checksum_name=f"personal-codex-{SHA1}.sha256",
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
                    ),
                    release_root=public_release,
                )
            raise AssertionError(f"unexpected repo: {repo}")

        with (
            mock.patch.object(MODULE, "download_and_extract_release", fake_download),
            mock.patch.object(
                MODULE,
                "_validate_release_manifest_owner",
                side_effect=validate_then_replace_manifest,
            ),
        ):
            self.run_quietly(
                MODULE.install_private_from_github,
                "Joey-Tools/codex-private-workflows",
                home,
                base_repo="Fallback/base",
                owner="private",
                dry_run=False,
            )

        self.assertTrue(overlay_validated)
        self.assertEqual(
            downloads,
            [
                ("Joey-Tools/codex-private-workflows", None),
                ("Joey-Tools/codex-toolbox", None),
            ],
        )
        self.assertEqual(manifest_path.read_bytes(), original_manifest)

    def test_install_private_transfers_removed_private_skill_to_public(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        new_public = self.root / "new-public"
        new_private = self.root / "new-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper", "moving-skill"),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        write_skill_manifest_release(
            new_public,
            skills=("public-base", "moving-skill"),
        )
        write_skill_manifest_release(
            new_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[
                {
                    "id": "move-moving-skill-to-public",
                    "source": "personal_codex/skills/moving-skill",
                    "target": "skills/moving-skill",
                    "kind": "skill",
                    "replacement_target": "skills/moving-skill",
                }
            ],
        )

        with self.capture_reconcile_backups() as backup_events:
            self.install_private_pair(
                home,
                new_public,
                new_private,
                public_sha=SHA3,
                private_sha=SHA4,
            )

        moving_link = home / "skills" / "moving-skill"
        self.assertEqual(
            moving_link.readlink().as_posix(),
            "../personal-sync/current/personal_codex/skills/moving-skill",
        )
        self.assertEqual(
            list(
                (home / "personal-sync" / "quarantine").glob(
                    "*/links/skills/moving-skill"
                )
            ),
            [],
        )
        self.assertIn(
            (
                "replace",
                "skills/moving-skill",
                "../personal-sync/overlays/private/current/"
                "personal_codex/skills/moving-skill",
                None,
            ),
            backup_events,
        )
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        moving_record = next(
            entry for entry in state["links"] if entry["target"] == "skills/moving-skill"
        )
        self.assertEqual(moving_record["owner"], "public")

    def test_install_private_rejects_unavailable_active_replacement(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        new_public = self.root / "new-public"
        new_private = self.root / "new-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper", "moving-skill"),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        write_skill_manifest_release(new_public, skills=("public-base",))
        write_skill_manifest_release(
            new_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[
                {
                    "id": "move-moving-skill-to-public",
                    "source": "personal_codex/skills/moving-skill",
                    "target": "skills/moving-skill",
                    "kind": "skill",
                    "replacement_target": "skills/moving-skill",
                }
            ],
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "replacement target .* unavailable",
        ):
            self.install_private_pair(
                home,
                new_public,
                new_private,
                public_sha=SHA3,
                private_sha=SHA4,
            )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(
            (
                home
                / "personal-sync"
                / "overlays"
                / "private"
                / "current"
            ).readlink().as_posix(),
            f"releases/{SHA2}",
        )
        self.assertEqual(
            (home / "skills" / "moving-skill").readlink().as_posix(),
            "../personal-sync/overlays/private/current/personal_codex/skills/moving-skill",
        )

    def test_historical_replacement_may_later_be_removed(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        middle_public = self.root / "middle-public"
        middle_private = self.root / "middle-private"
        final_public = self.root / "final-public"
        final_private = self.root / "final-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper", "moving-skill"),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        private_removal = {
            "id": "move-moving-skill-to-public",
            "source": "personal_codex/skills/moving-skill",
            "target": "skills/moving-skill",
            "kind": "skill",
            "replacement_target": "skills/moving-skill",
        }
        write_skill_manifest_release(
            middle_public,
            skills=("public-base", "moving-skill"),
        )
        write_skill_manifest_release(
            middle_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[private_removal],
        )
        self.install_private_pair(
            home,
            middle_public,
            middle_private,
            public_sha=SHA3,
            private_sha=SHA4,
        )
        write_skill_manifest_release(
            final_public,
            skills=("public-base",),
            removed_links=[
                {
                    "id": "remove-public-moving-skill",
                    "source": "personal_codex/skills/moving-skill",
                    "target": "skills/moving-skill",
                    "kind": "skill",
                    "retires_replacements": [
                        "private:move-moving-skill-to-public"
                    ],
                }
            ],
        )
        write_skill_manifest_release(
            final_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[private_removal],
        )

        self.install_private_pair(
            home,
            final_public,
            final_private,
            public_sha=SHA5,
            private_sha=SHA6,
        )

        self.assertFalse(os.path.lexists(home / "skills" / "moving-skill"))
        self.assertEqual(current_target(home), f"releases/{SHA5}")

    def test_skip_version_allows_replacement_with_later_removal(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        final_public = self.root / "final-public"
        final_private = self.root / "final-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper", "moving-skill"),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        write_skill_manifest_release(
            final_public,
            skills=("public-base",),
            removed_links=[
                {
                    "id": "remove-public-moving-skill",
                    "source": "personal_codex/skills/moving-skill",
                    "target": "skills/moving-skill",
                    "kind": "skill",
                    "retires_replacements": [
                        "private:move-moving-skill-to-public"
                    ],
                }
            ],
        )
        write_skill_manifest_release(
            final_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[
                {
                    "id": "move-moving-skill-to-public",
                    "source": "personal_codex/skills/moving-skill",
                    "target": "skills/moving-skill",
                    "kind": "skill",
                    "replacement_target": "skills/moving-skill",
                }
            ],
        )

        self.install_private_pair(
            home,
            final_public,
            final_private,
            public_sha=SHA3,
            private_sha=SHA4,
        )

        self.assertFalse(os.path.lexists(home / "skills" / "moving-skill"))
        self.assertEqual(current_target(home), f"releases/{SHA3}")

    def test_install_private_rejects_unknown_replacement_retirement(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        private_release = self.root / "private"
        write_skill_manifest_release(
            public_release,
            skills=("public-base",),
            removed_links=[
                {
                    "id": "remove-public-moving-skill",
                    "source": "personal_codex/skills/moving-skill",
                    "target": "skills/moving-skill",
                    "kind": "skill",
                    "retires_replacements": ["private:missing"],
                }
            ],
        )
        write_skill_manifest_release(
            private_release,
            owner="private",
            skills=("private-keeper",),
        )

        with self.assertRaisesRegex(MODULE.SyncError, "retires unknown replacement"):
            self.install_private_pair(
                home,
                public_release,
                private_release,
                public_sha=SHA1,
                private_sha=SHA2,
            )

        self.assertFalse(os.path.lexists(home / "personal-sync" / "current"))

    def test_public_install_allows_retirement_for_absent_overlay(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        write_skill_manifest_release(
            public_release,
            skills=("public-base",),
            removed_links=[
                {
                    "id": "remove-public-moving-skill",
                    "source": "personal_codex/skills/moving-skill",
                    "target": "skills/moving-skill",
                    "kind": "skill",
                    "retires_replacements": [
                        "private:move-moving-skill-to-public"
                    ],
                }
            ],
        )

        self.run_quietly(
            MODULE.install_release_tree,
            public_release,
            home,
            SHA1,
            dry_run=False,
        )

        self.assertTrue((home / "skills" / "public-base").is_symlink())

    def test_install_private_reconciles_matching_legacy_private_symlink(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        new_public = self.root / "new-public"
        new_private = self.root / "new-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper",),
            extra_skill_dirs=("legacy-skill",),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        legacy_link = home / "skills" / "legacy-skill"
        legacy_link.symlink_to(
            "../personal-sync/overlays/private/current/personal_codex/skills/legacy-skill",
            target_is_directory=True,
        )
        write_skill_manifest_release(
            new_public,
            skills=("public-base", "legacy-skill"),
        )
        write_skill_manifest_release(
            new_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[
                {
                    "id": "retire-legacy-private-link",
                    "source": "personal_codex/skills/legacy-skill",
                    "target": "skills/legacy-skill",
                    "kind": "skill",
                    "legacy": True,
                }
            ],
        )

        with self.capture_reconcile_backups() as backup_events:
            self.install_private_pair(
                home,
                new_public,
                new_private,
                public_sha=SHA3,
                private_sha=SHA4,
            )

        self.assertEqual(
            legacy_link.readlink().as_posix(),
            "../personal-sync/current/personal_codex/skills/legacy-skill",
        )
        self.assertEqual(
            list(
                (home / "personal-sync" / "quarantine").glob(
                    "*/links/skills/legacy-skill"
                )
            ),
            [],
        )
        self.assertIn(
            (
                "quarantine-replace",
                "skills/legacy-skill",
                "../personal-sync/overlays/private/current/"
                "personal_codex/skills/legacy-skill",
                "private:retire-legacy-private-link",
            ),
            backup_events,
        )

    def test_install_private_preserves_local_directory_at_removed_target(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        new_public = self.root / "new-public"
        new_private = self.root / "new-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper",),
            extra_skill_dirs=("legacy-skill",),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        local_directory = home / "skills" / "legacy-skill"
        local_directory.mkdir()
        (local_directory / "local.txt").write_text("local\n", encoding="utf-8")
        write_skill_manifest_release(
            new_public,
            skills=("public-base", "legacy-skill"),
        )
        write_skill_manifest_release(
            new_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[
                {
                    "id": "retire-legacy-private-link",
                    "source": "personal_codex/skills/legacy-skill",
                    "target": "skills/legacy-skill",
                    "kind": "skill",
                    "legacy": True,
                }
            ],
        )
        quarantine_root = home / "personal-sync" / "quarantine"
        quarantine_before = snapshot_tree(quarantine_root)

        with self.assertRaisesRegex(MODULE.SyncError, "non-symlink target"):
            self.install_private_pair(
                home,
                new_public,
                new_private,
                public_sha=SHA3,
                private_sha=SHA4,
            )

        self.assertEqual((local_directory / "local.txt").read_text(encoding="utf-8"), "local\n")
        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(snapshot_tree(quarantine_root), quarantine_before)

    def test_install_private_does_not_commit_state_after_overlay_verification_failure(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        new_public = self.root / "new-public"
        new_private = self.root / "new-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper",),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        old_state = state_path.read_bytes()
        write_skill_manifest_release(new_public, skills=("public-base",))
        write_skill_manifest_release(
            new_private,
            owner="private",
            skills=("private-keeper",),
        )

        with mock.patch.object(MODULE, "_collect_overlay_issues", return_value=["forced"]):
            with self.assertRaisesRegex(MODULE.SyncError, "overlay verification failed"):
                self.install_private_pair(
                    home,
                    new_public,
                    new_private,
                    public_sha=SHA3,
                    private_sha=SHA4,
                )

        self.assertEqual(state_path.read_bytes(), old_state)

        self.install_private_pair(
            home,
            new_public,
            new_private,
            public_sha=SHA3,
            private_sha=SHA4,
        )
        recovered_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            recovered_state["owners"],
            {"private": SHA4, "public": SHA3},
        )

    def test_install_private_rejects_cross_layer_ancestor_target_collision(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        private_release = self.root / "private"
        write_skill_manifest_release(public_release, skills=("parent",))
        write_skill_manifest_release(
            private_release,
            owner="private",
            skills=("child",),
        )
        manifest_path = private_release / "personal_codex" / "sync-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["links"][0]["target"] = "skills/parent/child"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(MODULE.SyncError, "must not overlap"):
            self.install_private_pair(
                home,
                public_release,
                private_release,
                public_sha=SHA1,
                private_sha=SHA2,
            )

        self.assertFalse((home / "personal-sync" / "current").exists())
        self.assertFalse((home / "skills").exists())

    def test_install_private_quarantine_remove_reapplies_if_legacy_link_returns(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        new_public = self.root / "new-public"
        new_private = self.root / "new-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper",),
            extra_skill_dirs=("legacy-skill",),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        legacy_link = home / "skills" / "legacy-skill"
        legacy_target = (
            "../personal-sync/overlays/private/current/"
            "personal_codex/skills/legacy-skill"
        )
        legacy_link.symlink_to(legacy_target, target_is_directory=True)
        removed_links = [
            {
                "id": "retire-legacy-private-link",
                "source": "personal_codex/skills/legacy-skill",
                "target": "skills/legacy-skill",
                "kind": "skill",
                "legacy": True,
            }
        ]
        write_skill_manifest_release(new_public, skills=("public-base",))
        write_skill_manifest_release(
            new_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=removed_links,
            extra_skill_dirs=("legacy-skill",),
        )

        with self.capture_reconcile_backups() as first_backup_events:
            self.install_private_pair(
                home,
                new_public,
                new_private,
                public_sha=SHA3,
                private_sha=SHA4,
            )
        self.assertFalse(os.path.lexists(legacy_link))
        legacy_link.symlink_to(legacy_target, target_is_directory=True)

        with self.capture_reconcile_backups() as second_backup_events:
            self.install_private_pair(
                home,
                new_public,
                new_private,
                public_sha=SHA3,
                private_sha=SHA4,
            )

        self.assertFalse(os.path.lexists(legacy_link))
        self.assertEqual(
            list(
                (home / "personal-sync" / "quarantine").glob(
                    "*/links/skills/legacy-skill"
                )
            ),
            [],
        )
        expected_backup = (
            "quarantine-remove",
            "skills/legacy-skill",
            legacy_target,
            "private:retire-legacy-private-link",
        )
        self.assertIn(expected_backup, first_backup_events)
        self.assertIn(expected_backup, second_backup_events)

    def test_install_private_legacy_quarantine_dry_run_has_no_side_effects(self) -> None:
        home = self.root / "home" / ".codex"
        old_public = self.root / "old-public"
        old_private = self.root / "old-private"
        new_public = self.root / "new-public"
        new_private = self.root / "new-private"
        write_skill_manifest_release(old_public, skills=("public-base",))
        write_skill_manifest_release(
            old_private,
            owner="private",
            skills=("private-keeper",),
            extra_skill_dirs=("legacy-skill",),
        )
        self.install_private_pair(
            home,
            old_public,
            old_private,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        old_state = state_path.read_bytes()
        legacy_link = home / "skills" / "legacy-skill"
        legacy_target = (
            "../personal-sync/overlays/private/current/"
            "personal_codex/skills/legacy-skill"
        )
        legacy_link.symlink_to(legacy_target, target_is_directory=True)
        write_skill_manifest_release(
            new_public,
            skills=("public-base", "legacy-skill"),
        )
        write_skill_manifest_release(
            new_private,
            owner="private",
            skills=("private-keeper",),
            removed_links=[
                {
                    "id": "retire-legacy-private-link",
                    "source": "personal_codex/skills/legacy-skill",
                    "target": "skills/legacy-skill",
                    "kind": "skill",
                    "legacy": True,
                }
            ],
        )
        quarantine_root = home / "personal-sync" / "quarantine"
        quarantine_before = snapshot_tree(quarantine_root)

        self.install_private_pair(
            home,
            new_public,
            new_private,
            public_sha=SHA3,
            private_sha=SHA4,
            dry_run=True,
        )

        self.assertEqual(legacy_link.readlink().as_posix(), legacy_target)
        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(
            (home / "personal-sync" / "overlays" / "private" / "current")
            .readlink()
            .as_posix(),
            f"releases/{SHA2}",
        )
        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(snapshot_tree(quarantine_root), quarantine_before)
        self.assertFalse((home / "personal-sync" / "releases" / SHA3).exists())
        self.assertFalse(
            (
                home
                / "personal-sync"
                / "overlays"
                / "private"
                / "releases"
                / SHA4
            ).exists()
        )

    def test_uninstall_overlay_restores_public_links_and_updates_state(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        private_release = self.root / "private"
        write_skill_manifest_release(
            public_release,
            skills=("public-base", "shared"),
        )
        write_skill_manifest_release(
            private_release,
            owner="private",
            skills=("private-only", "shared"),
        )
        manifest_path = private_release / "personal_codex" / "sync-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        next(
            entry
            for entry in manifest["links"]
            if entry["target"] == "skills/shared"
        )["override"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.install_private_pair(
            home,
            public_release,
            private_release,
            public_sha=SHA1,
            private_sha=SHA2,
        )

        self.run_quietly(MODULE.uninstall_overlay, home, "private", dry_run=False)

        self.assertFalse(
            (home / "personal-sync" / "overlays" / "private" / "current").exists()
        )
        self.assertFalse(os.path.lexists(home / "skills" / "private-only"))
        self.assertEqual(
            (home / "skills" / "shared").readlink().as_posix(),
            "../personal-sync/current/personal_codex/skills/shared",
        )
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertEqual(
            {entry["owner"] for entry in state["links"]},
            {"public"},
        )

    def test_uninstall_overlay_rolls_back_then_retries_after_write_failure(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        private_release = self.root / "private"
        write_skill_manifest_release(public_release, skills=("public-base",))
        write_skill_manifest_release(
            private_release,
            owner="private",
            skills=("private-only",),
        )
        self.install_private_pair(
            home,
            public_release,
            private_release,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        old_state = state_path.read_bytes()

        with mock.patch.object(
            MODULE,
            "_write_managed_state",
            side_effect=MODULE.SyncError("forced state write failure"),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "forced state write failure"):
                self.run_quietly(
                    MODULE.uninstall_overlay,
                    home,
                    "private",
                    dry_run=False,
                )

        self.assertTrue(
            (home / "personal-sync" / "overlays" / "private" / "current").is_symlink()
        )
        self.assertTrue((home / "skills" / "private-only").is_symlink())
        self.assertEqual(state_path.read_bytes(), old_state)

        self.run_quietly(MODULE.uninstall_overlay, home, "private", dry_run=False)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertEqual(
            {entry["owner"] for entry in state["links"]},
            {"public"},
        )

    def test_uninstall_overlay_succeeds_when_current_pointer_is_missing(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        private_release = self.root / "private"
        write_skill_manifest_release(public_release, skills=("public-base",))
        write_skill_manifest_release(
            private_release,
            owner="private",
            skills=("private-only",),
        )
        self.install_private_pair(
            home,
            public_release,
            private_release,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        private_current = (
            home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_current.unlink()

        self.run_quietly(MODULE.uninstall_overlay, home, "private", dry_run=False)

        self.assertFalse(os.path.lexists(private_current))
        self.assertFalse(os.path.lexists(home / "skills" / "private-only"))
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertEqual(
            {entry["owner"] for entry in state["links"]},
            {"public"},
        )

    def test_uninstall_overlay_with_missing_current_rolls_back_and_retries(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        private_release = self.root / "private"
        write_skill_manifest_release(public_release, skills=("public-base",))
        write_skill_manifest_release(
            private_release,
            owner="private",
            skills=("private-only",),
        )
        self.install_private_pair(
            home,
            public_release,
            private_release,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        private_current = (
            home / "personal-sync" / "overlays" / "private" / "current"
        )
        private_current.unlink()
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        old_state = state_path.read_bytes()

        real_clear_pending = MODULE._clear_pending_link_pointer

        def fail_precommit_pointer_clear(
            pending_home: Path,
            batch: object,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "before":
                raise MODULE.SyncError("forced pending pointer failure")
            real_clear_pending(pending_home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_write_managed_state",
                side_effect=MODULE.SyncError("forced state write failure"),
            ),
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=fail_precommit_pointer_clear,
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete: pending pointer: forced pending pointer failure",
            ):
                self.run_quietly(
                    MODULE.uninstall_overlay,
                    home,
                    "private",
                    dry_run=False,
                )

        self.assertFalse(os.path.lexists(private_current))
        self.assertTrue((home / "skills" / "private-only").is_symlink())
        self.assertEqual(state_path.read_bytes(), old_state)
        pointer = MODULE._pending_link_pointer_path(home)
        self.assertTrue(pointer.is_file())
        batch = MODULE._load_pending_link_batch(home)
        assert batch is not None
        retired_current_records = [
            record
            for record in batch.records
            if record.scope == "current" and record.action == "retire-absent"
        ]
        self.assertEqual(len(retired_current_records), 1)
        retired_current = retired_current_records[0]
        self.assertEqual(retired_current.owner, "private")
        self.assertFalse(
            any(
                claim.scope == "current" and claim.owner == "private"
                for claim in batch.claims_before
            )
        )

        private_current.symlink_to(f"releases/{SHA2}", target_is_directory=True)
        foreign_current_identity = (
            private_current.lstat().st_dev,
            private_current.lstat().st_ino,
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending retired absence found a foreign target",
        ):
            self.run_quietly(MODULE.uninstall_overlay, home, "private", dry_run=False)
        self.assertTrue(pointer.is_file())
        self.assertEqual(
            (private_current.lstat().st_dev, private_current.lstat().st_ino),
            foreign_current_identity,
        )
        private_current.unlink()

        pointer_payload = pointer.read_bytes()
        malformed = json.loads(pointer_payload)
        retired_payload = next(
            record
            for record in malformed["records"]
            if record["scope"] == "current"
            and record["action"] == "retire-absent"
        )
        retired_payload["owner"] = "ghost"
        retired_payload["target"] = (
            "personal-sync/overlays/ghost/current"
        )
        pointer.write_text(json.dumps(malformed) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "retired current target is not an exact state transition",
        ):
            MODULE._load_pending_link_batch(home)
        pointer.write_bytes(pointer_payload)

        self.run_quietly(MODULE.uninstall_overlay, home, "private", dry_run=False)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertFalse(os.path.lexists(home / "skills" / "private-only"))

    def test_uninstall_overlay_retains_pending_when_outgoing_release_changes(self) -> None:
        home = self.root / "home" / ".codex"
        public_release = self.root / "public"
        private_release = self.root / "private"
        write_skill_manifest_release(public_release, skills=("public-base",))
        write_skill_manifest_release(
            private_release,
            owner="private",
            skills=("private-only",),
        )
        self.install_private_pair(
            home,
            public_release,
            private_release,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        private_current = (
            home / "personal-sync" / "overlays" / "private" / "current"
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        old_state = state_path.read_bytes()
        installed_skill = (
            home
            / "personal-sync"
            / "overlays"
            / "private"
            / "releases"
            / SHA2
            / "personal_codex"
            / "skills"
            / "private-only"
            / "SKILL.md"
        )

        def change_outgoing_release_then_fail(*_args: object) -> None:
            installed_skill.chmod(0o644)
            installed_skill.write_text("tampered\n", encoding="utf-8")
            raise MODULE.SyncError("forced state write failure")

        with mock.patch.object(
            MODULE,
            "_write_managed_state",
            side_effect=change_outgoing_release_then_fail,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "rollback was incomplete: releases: release tree changed",
            ):
                self.run_quietly(
                    MODULE.uninstall_overlay,
                    home,
                    "private",
                    dry_run=False,
                )

        self.assertTrue(private_current.is_symlink())
        self.assertTrue((home / "skills" / "private-only").is_symlink())
        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertTrue(MODULE._pending_link_pointer_path(home).is_file())

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
                {"id": 101, "name": f"personal-codex-{SHA1}.tar.gz", "size": 1},
                {"id": 102, "name": f"personal-codex-{SHA1}.sha256", "size": 1},
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.sha, SHA1)
        self.assertEqual(assets.archive_name, f"personal-codex-{SHA1}.tar.gz")
        self.assertEqual((assets.archive_id, assets.archive_size), (101, 1))
        self.assertEqual(assets.checksum_name, f"personal-codex-{SHA1}.sha256")
        self.assertEqual((assets.checksum_id, assets.checksum_size), (102, 1))

    def test_select_release_assets_rejects_invalid_api_metadata(self) -> None:
        archive_name = f"personal-codex-{SHA1}.tar.gz"
        checksum_name = f"personal-codex-{SHA1}.sha256"
        cases = (
            ("missing-id", {"name": archive_name, "size": 1}, "asset id"),
            ("boolean-id", {"id": True, "name": archive_name, "size": 1}, "asset id"),
            ("zero-id", {"id": 0, "name": archive_name, "size": 1}, "asset id"),
            ("missing-size", {"id": 101, "name": archive_name}, "asset size"),
            (
                "boolean-size",
                {"id": 101, "name": archive_name, "size": False},
                "asset size",
            ),
            (
                "negative-size",
                {"id": 101, "name": archive_name, "size": -1},
                "asset size",
            ),
            (
                "oversized",
                {
                    "id": 101,
                    "name": archive_name,
                    "size": MODULE.MAX_ARCHIVE_COMPRESSED_BYTES + 1,
                },
                "exceeds",
            ),
        )
        checksum = {"id": 102, "name": checksum_name, "size": 1}

        for name, archive, error_pattern in cases:
            with self.subTest(name=name):
                release = {
                    "tagName": "personal-codex-20260511-120000-1111111",
                    "targetCommitish": SHA1,
                    "assets": [archive, checksum],
                }
                with self.assertRaisesRegex(MODULE.SyncError, error_pattern):
                    MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_missing_checksum(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "assets": [
                {"id": 101, "name": f"personal-codex-{SHA1}.tar.gz", "size": 1}
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "missing checksum"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_multiple_tarballs(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "assets": [
                {"id": 101, "name": f"personal-codex-{SHA1}.tar.gz", "size": 1},
                {"id": 201, "name": f"personal-codex-{SHA2}.tar.gz", "size": 1},
                {"id": 102, "name": f"personal-codex-{SHA1}.sha256", "size": 1},
                {"id": 202, "name": f"personal-codex-{SHA2}.sha256", "size": 1},
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
                {"id": 101, "name": f"personal-codex-{SHA1}.tar.gz", "size": 1},
                {"id": 102, "name": f"personal-codex-{SHA1}.sha256", "size": 1},
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.archive_name, f"personal-codex-{SHA1}.tar.gz")
        self.assertEqual(assets.checksum_name, f"personal-codex-{SHA1}.sha256")

    def test_select_release_assets_rejects_tag_sha_mismatch(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-2222222",
            "assets": [
                {"id": 101, "name": f"personal-codex-{SHA1}.tar.gz", "size": 1},
                {"id": 102, "name": f"personal-codex-{SHA1}.sha256", "size": 1},
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "does not match tag suffix"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_target_commit_mismatch(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA2,
            "assets": [
                {"id": 101, "name": f"personal-codex-{SHA1}.tar.gz", "size": 1},
                {"id": 102, "name": f"personal-codex-{SHA1}.sha256", "size": 1},
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "does not match target commit"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_accepts_github_api_payload(self) -> None:
        release = {
            "tag_name": "personal-codex-20260511-120000-1111111",
            "target_commitish": SHA1,
            "assets": [
                {"id": 101, "name": f"personal-codex-{SHA1}.tar.gz", "size": 1},
                {"id": 102, "name": f"personal-codex-{SHA1}.sha256", "size": 1},
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

    def test_download_release_assets_streams_api_assets_by_id(self) -> None:
        archive_payload = b"archive-payload"
        checksum_payload = b"checksum-payload"
        calls: list[list[str]] = []
        processes = [
            FakeDownloadProcess(archive_payload),
            FakeDownloadProcess(checksum_payload),
        ]
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=len(archive_payload),
            checksum_id=102,
            checksum_size=len(checksum_payload),
        )

        def fake_popen(args, **_kwargs):
            calls.append(args)
            return processes.pop(0)

        destination = self.root / "downloads"
        with mock.patch.object(MODULE.subprocess, "Popen", side_effect=fake_popen):
            MODULE.download_release_assets("owner/repo", assets, destination)

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][:3],
            ["gh", "api", "repos/owner/repo/releases/assets/101"],
        )
        self.assertEqual(
            calls[1][:3],
            ["gh", "api", "repos/owner/repo/releases/assets/102"],
        )
        self.assertIn("Accept: application/octet-stream", calls[0])
        self.assertEqual((destination / assets.archive_name).read_bytes(), archive_payload)
        self.assertEqual((destination / assets.checksum_name).read_bytes(), checksum_payload)
        self.assertEqual(list(destination.glob(".*.partial.*")), [])

    def test_download_release_assets_rejects_replaced_partial_during_publish(
        self,
    ) -> None:
        payload = b"trusted-payload"
        process = FakeDownloadProcess(payload)
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=len(payload),
            checksum_id=102,
            checksum_size=1,
        )
        destination = self.root / "replaced-partial-download"
        real_link = os.link
        replaced = False

        def replace_partial_before_link(source, target, **kwargs):
            nonlocal replaced
            source_fd = kwargs["src_dir_fd"]
            if not replaced:
                replaced = True
                os.unlink(source, dir_fd=source_fd)
                replacement_fd = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_fd,
                )
                try:
                    os.write(replacement_fd, b"forged-payload")
                finally:
                    os.close(replacement_fd)
            return real_link(source, target, **kwargs)

        with (
            mock.patch.object(MODULE.subprocess, "Popen", return_value=process),
            mock.patch.object(MODULE.os, "link", side_effect=replace_partial_before_link),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed during publication",
            ),
        ):
            MODULE.download_release_assets("owner/repo", assets, destination)

        self.assertTrue(replaced)
        self.assertFalse((destination / assets.archive_name).exists())
        retained = list(destination.iterdir())
        self.assertGreaterEqual(len(retained), 1)
        self.assertTrue(
            all(
                entry.name.startswith(".codex-extract-retained-download-")
                for entry in retained
            )
        )
        self.assertEqual({entry.read_bytes() for entry in retained}, {b"forged-payload"})

    def test_download_release_assets_rejects_replaced_target_during_publish(
        self,
    ) -> None:
        payload = b"trusted-payload"
        process = FakeDownloadProcess(payload)
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=len(payload),
            checksum_id=102,
            checksum_size=1,
        )
        destination = self.root / "replaced-target-download"
        real_link = os.link
        replaced = False

        def replace_target_after_link(source, target, **kwargs):
            nonlocal replaced
            result = real_link(source, target, **kwargs)
            if not replaced:
                replaced = True
                target_fd = kwargs["dst_dir_fd"]
                os.unlink(target, dir_fd=target_fd)
                replacement_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=target_fd,
                )
                try:
                    os.write(replacement_fd, b"forged-payload")
                finally:
                    os.close(replacement_fd)
            return result

        with (
            mock.patch.object(MODULE.subprocess, "Popen", return_value=process),
            mock.patch.object(MODULE.os, "link", side_effect=replace_target_after_link),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed during publication",
            ),
        ):
            MODULE.download_release_assets("owner/repo", assets, destination)

        self.assertTrue(replaced)
        self.assertFalse((destination / assets.archive_name).exists())
        retained = list(destination.iterdir())
        self.assertGreaterEqual(len(retained), 1)
        self.assertTrue(
            all(
                entry.name.startswith(".codex-extract-retained-download-")
                for entry in retained
            )
        )
        self.assertEqual({entry.read_bytes() for entry in retained}, {b"forged-payload"})

    def test_download_release_assets_preserves_partial_replaced_during_cleanup(
        self,
    ) -> None:
        payload = b"trusted-payload"
        process = FakeDownloadProcess(payload)
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=len(payload),
            checksum_id=102,
            checksum_size=1,
        )
        destination = self.root / "replaced-partial-cleanup"
        real_rename_noreplace = MODULE._rename_noreplace_at
        replaced = False

        def replace_partial_before_cleanup(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        ):
            nonlocal replaced
            if source_name.startswith(".codex-extract-download-") and not replaced:
                replaced = True
                os.unlink(source_name, dir_fd=source_parent_fd)
                replacement_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(replacement_fd, b"forged-payload")
                finally:
                    os.close(replacement_fd)
            return real_rename_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(MODULE.subprocess, "Popen", return_value=process),
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=replace_partial_before_cleanup,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "partial release asset changed during cleanup",
            ),
        ):
            MODULE.download_release_assets("owner/repo", assets, destination)

        self.assertTrue(replaced)
        self.assertFalse((destination / assets.archive_name).exists())
        retained = list(destination.iterdir())
        self.assertGreaterEqual(len(retained), 1)
        self.assertTrue(
            all(
                entry.name.startswith(".codex-extract-retained-download-")
                for entry in retained
            )
        )
        self.assertEqual({entry.read_bytes() for entry in retained}, {b"forged-payload"})

    def test_download_release_assets_cleanup_error_does_not_mask_primary_error(
        self,
    ) -> None:
        process = FakeDownloadProcess(b"123")
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=4,
            checksum_id=102,
            checksum_size=1,
        )
        destination = self.root / "cleanup-error-download"
        real_fsync = os.fsync

        def fail_directory_fsync(file_descriptor):
            if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                raise OSError("injected directory fsync failure")
            return real_fsync(file_descriptor)

        with (
            mock.patch.object(MODULE.subprocess, "Popen", return_value=process),
            mock.patch.object(MODULE.os, "fsync", side_effect=fail_directory_fsync),
            mock.patch.object(
                MODULE,
                "_close_fd_quietly",
                wraps=MODULE._close_fd_quietly,
            ) as close_fd,
            self.assertRaisesRegex(MODULE.SyncError, "size mismatch"),
        ):
            MODULE.download_release_assets("owner/repo", assets, destination)

        self.assertGreaterEqual(close_fd.call_count, 2)
        for call in close_fd.call_args_list[-2:]:
            with self.assertRaises(OSError):
                os.fstat(call.args[0])
        self.assertFalse((destination / assets.archive_name).exists())
        retained = list(destination.iterdir())
        self.assertEqual(len(retained), 1)
        self.assertTrue(
            retained[0].name.startswith(".codex-extract-retained-download-")
        )
        self.assertEqual(retained[0].read_bytes(), b"123")

    def test_download_release_assets_terminates_stream_over_advertised_size(
        self,
    ) -> None:
        process = FakeDownloadProcess(b"12345")
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=4,
            checksum_id=102,
            checksum_size=1,
        )
        destination = self.root / "oversized-download"

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(MODULE.SyncError, "exceeds its advertised"):
                MODULE.download_release_assets("owner/repo", assets, destination)

        self.assertTrue(process.terminated)
        self.assertFalse((destination / assets.archive_name).exists())
        self.assertEqual(list(destination.glob(".*.partial.*")), [])

    def test_download_release_assets_rejects_short_stream_and_removes_partial(
        self,
    ) -> None:
        process = FakeDownloadProcess(b"123")
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=4,
            checksum_id=102,
            checksum_size=1,
        )
        destination = self.root / "short-download"

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(MODULE.SyncError, "size mismatch"):
                MODULE.download_release_assets("owner/repo", assets, destination)

        self.assertFalse((destination / assets.archive_name).exists())
        self.assertEqual(list(destination.glob(".*.partial.*")), [])

    def test_download_release_assets_validates_all_metadata_before_starting(self) -> None:
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=1,
            checksum_id=102,
            checksum_size=MODULE.MAX_ARCHIVE_CHECKSUM_BYTES + 1,
        )
        destination = self.root / "invalid-metadata-download"

        with mock.patch.object(MODULE.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(MODULE.SyncError, "exceeds"):
                MODULE.download_release_assets("owner/repo", assets, destination)

        popen.assert_not_called()
        self.assertFalse(destination.exists())

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

    def test_verify_checksum_enforces_input_size_limits(self) -> None:
        archive = self.root / f"personal-codex-{SHA1}.tar.gz"
        checksum = self.root / f"personal-codex-{SHA1}.sha256"
        archive.write_bytes(b"archive")
        digest = hashlib.sha256(b"archive").hexdigest()
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")

        with (
            self.subTest(limit="checksum"),
            mock.patch.object(MODULE, "MAX_ARCHIVE_CHECKSUM_BYTES", 1),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "checksum file exceeds"):
                MODULE.verify_checksum(archive, checksum)

        with (
            self.subTest(limit="compressed"),
            mock.patch.object(MODULE, "MAX_ARCHIVE_COMPRESSED_BYTES", 1),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "compressed archive exceeds"):
                MODULE.verify_checksum(archive, checksum)

    def test_verify_checksum_rejects_symlink_and_fifo_inputs(self) -> None:
        for role in ("archive", "checksum"):
            for kind in ("symlink", "fifo"):
                with self.subTest(role=role, kind=kind):
                    case_root = self.root / f"unsafe-{role}-{kind}"
                    case_root.mkdir()
                    archive = case_root / f"personal-codex-{SHA1}.tar.gz"
                    checksum = case_root / f"personal-codex-{SHA1}.sha256"
                    archive_payload = b"archive"
                    archive.write_bytes(archive_payload)
                    digest = hashlib.sha256(archive_payload).hexdigest()
                    checksum.write_text(
                        f"{digest}  {archive.name}\n",
                        encoding="utf-8",
                    )
                    unsafe_path = archive if role == "archive" else checksum
                    unsafe_path.unlink()
                    if kind == "symlink":
                        backing = case_root / f"{role}-backing"
                        if role == "archive":
                            backing.write_bytes(archive_payload)
                        else:
                            backing.write_text(
                                f"{digest}  {archive.name}\n",
                                encoding="utf-8",
                            )
                        unsafe_path.symlink_to(backing)
                    else:
                        os.mkfifo(unsafe_path)

                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "unsafe|non-regular",
                    ):
                        MODULE.verify_checksum(archive, checksum)

    def test_verify_checksum_rejects_same_inode_archive_rewrite(self) -> None:
        archive = self.root / f"personal-codex-{SHA1}.tar.gz"
        checksum = self.root / f"personal-codex-{SHA1}.sha256"
        archive_payload = b"archive"
        archive.write_bytes(archive_payload)
        digest = hashlib.sha256(archive_payload).hexdigest()
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        archive_identity = archive.stat().st_ino, archive.stat().st_size
        real_read = os.read
        rewritten = False

        def rewrite_after_archive_read(file_descriptor, size):
            nonlocal rewritten
            payload = real_read(file_descriptor, size)
            metadata = os.fstat(file_descriptor)
            if metadata.st_ino == archive_identity[0] and payload and not rewritten:
                rewritten = True
                writer_fd = os.open(archive, os.O_RDWR)
                try:
                    os.lseek(writer_fd, -1, os.SEEK_END)
                    os.write(writer_fd, b"X")
                    os.fsync(writer_fd)
                finally:
                    os.close(writer_fd)
            return payload

        with mock.patch.object(MODULE.os, "read", rewrite_after_archive_read):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "compressed archive changed while reading",
            ):
                MODULE.verify_checksum(archive, checksum)

        self.assertTrue(rewritten)
        self.assertEqual((archive.stat().st_ino, archive.stat().st_size), archive_identity)

    def test_download_extracts_verified_snapshot_after_archive_path_replacement(
        self,
    ) -> None:
        destination = self.root / "download"
        destination.mkdir()
        source_root = self.root / "snapshot-source"
        write_minimal_release(source_root, agent_text="verified\n")
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260520-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=1,
            archive_size=1,
            checksum_id=2,
            checksum_size=1,
        )
        archive_path = destination / assets.archive_name
        checksum_path = destination / assets.checksum_name
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        checksum_path.write_text(
            f"{digest}  {archive_path.name}\n",
            encoding="utf-8",
        )
        malicious_archive = self.root / "replacement.tar.gz"
        malicious_archive.write_bytes(b"replacement")
        retained_archive = self.root / "verified.tar.gz"
        real_extract = MODULE._safe_extract_archive_snapshot

        def replace_archive_path(snapshot, extract_root):
            archive_path.rename(retained_archive)
            malicious_archive.rename(archive_path)
            return real_extract(snapshot, extract_root)

        with (
            mock.patch.object(MODULE, "find_latest_release", return_value={}),
            mock.patch.object(MODULE, "select_release_assets", return_value=assets),
            mock.patch.object(MODULE, "download_release_assets"),
            mock.patch.object(
                MODULE,
                "_safe_extract_archive_snapshot",
                side_effect=replace_archive_path,
            ),
        ):
            release = MODULE.download_and_extract_release("owner/repo", destination)

        self.assertEqual(
            (release.release_root / "personal_codex" / "AGENTS.md").read_text(
                encoding="utf-8"
            ),
            "verified\n",
        )
        self.assertIsNotNone(release.release_expectation)
        release_metadata = release.release_root.stat()
        self.assertEqual(
            release.release_expectation[1],
            (release_metadata.st_dev, release_metadata.st_ino),
        )
        self.assertRegex(release.release_expectation[0][2], r"^[0-9a-f]{64}$")
        self.assertEqual(archive_path.read_bytes(), b"replacement")
        self.assertTrue(retained_archive.is_file())

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

    def test_safe_extract_enforces_member_resource_limits_before_writing(self) -> None:
        cases = (
            ("members", 2, (b"a", b"b")),
            ("member-bytes", 1, (b"long",)),
            ("total-bytes", 2, (b"abc", b"def")),
        )
        for name, member_count, payloads in cases:
            with self.subTest(limit=name):
                archive_path = self.root / f"limit-{name}.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    for index in range(member_count):
                        payload = payloads[index]
                        member = tarfile.TarInfo(f"root/file-{index}.txt")
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
                destination = self.root / f"limit-{name}-extract"
                if name == "members":
                    patches = (mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBERS", 1),)
                    expected = "member limit"
                elif name == "member-bytes":
                    patches = (
                        mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBER_BYTES", 3),
                    )
                    expected = "member exceeds expanded byte limit"
                else:
                    patches = (
                        mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBER_BYTES", 10),
                        mock.patch.object(MODULE, "MAX_ARCHIVE_EXPANDED_BYTES", 5),
                    )
                    expected = "total expanded byte limit"
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaisesRegex(MODULE.SyncError, expected):
                        MODULE.safe_extract_archive(archive_path, destination)
                self.assertFalse(destination.exists())

    def test_safe_extract_counts_pax_metadata_against_expanded_limit(self) -> None:
        archive_path = self.root / "pax-metadata-limit.tar.gz"
        with tarfile.open(
            archive_path,
            "w:gz",
            format=tarfile.PAX_FORMAT,
        ) as archive:
            payload = b"x"
            member = tarfile.TarInfo("root/file.txt")
            member.size = len(payload)
            member.pax_headers = {"comment": "x" * 4096}
            archive.addfile(member, io.BytesIO(payload))
        destination = self.root / "pax-metadata-limit-extract"

        with mock.patch.object(MODULE, "MAX_ARCHIVE_EXPANDED_BYTES", 1024):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "total expanded byte limit",
            ):
                MODULE.safe_extract_archive(archive_path, destination)

        self.assertFalse(destination.exists())

    def test_safe_extract_counts_trailing_gzip_payload_against_expanded_limit(
        self,
    ) -> None:
        source_root = self.root / "trailing-payload-source"
        write_minimal_release(source_root)
        archive_path = self.root / "trailing-payload.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        archive_payload = archive_path.read_bytes()
        expanded_size = len(MODULE.gzip.decompress(archive_payload))
        archive_path.write_bytes(
            archive_payload + MODULE.gzip.compress(b"x" * 4096)
        )
        destination = self.root / "trailing-payload-extract"

        with mock.patch.object(
            MODULE,
            "MAX_ARCHIVE_EXPANDED_BYTES",
            expanded_size + 1024,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "total expanded byte limit",
            ):
                MODULE.safe_extract_archive(archive_path, destination)

        self.assertFalse(destination.exists())

    def test_safe_extract_rejects_same_inode_same_size_content_rewrite(self) -> None:
        source_root = self.root / "content-race-source"
        write_minimal_release(source_root, agent_text="agent\n")
        archive_path = self.root / "content-race.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        destination = self.root / "content-race-extract"
        real_identity = MODULE._release_tree_identity_from_directory_fd
        identity_calls = 0
        raced_path: Path | None = None
        original_identity: tuple[int, int] | None = None

        def rewrite_after_identity(
            root_fd,
            display_root,
            *,
            require_sanitized_modes=False,
        ):
            nonlocal identity_calls, raced_path, original_identity
            identity = real_identity(
                root_fd,
                display_root,
                require_sanitized_modes=require_sanitized_modes,
            )
            identity_calls += 1
            if identity_calls == 1:
                raced_path = display_root / "personal_codex" / "AGENTS.md"
                before = raced_path.stat()
                original_identity = before.st_ino, before.st_size
                file_descriptor = os.open(raced_path, os.O_RDWR)
                try:
                    os.write(file_descriptor, b"raced\n")
                    os.fsync(file_descriptor)
                finally:
                    os.close(file_descriptor)
                after = raced_path.stat()
                self.assertEqual((after.st_ino, after.st_size), original_identity)
            return identity

        with mock.patch.object(
            MODULE,
            "_release_tree_identity_from_directory_fd",
            side_effect=rewrite_after_identity,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "file changed during validation"):
                MODULE.safe_extract_archive(archive_path, destination)

        self.assertEqual(identity_calls, 1)
        self.assertIsNotNone(raced_path)
        self.assertEqual(raced_path.read_text(encoding="utf-8"), "raced\n")

    def test_safe_extract_rejects_normalized_member_aliases(self) -> None:
        for name, member_name in (
            ("empty", "personal-codex//payload.txt"),
            ("current", "personal-codex/./payload.txt"),
        ):
            with self.subTest(name=name):
                archive_path = self.root / f"unsafe-{name}.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    data = b"bad"
                    member = tarfile.TarInfo(member_name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))

                destination = self.root / f"unsafe-{name}-extract"
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "unsafe archive member path",
                ):
                    MODULE.safe_extract_archive(archive_path, destination)
                self.assertFalse(destination.exists())

    def test_safe_extract_rejects_pax_path_with_embedded_nul_before_writing(
        self,
    ) -> None:
        archive_path = self.root / "nul-path.tar.gz"
        with tarfile.open(
            archive_path,
            "w:gz",
            format=tarfile.PAX_FORMAT,
        ) as archive:
            payload = b"payload"
            member = tarfile.TarInfo("placeholder.txt")
            member.pax_headers = {"path": "personal-codex/nul\0payload.txt"}
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        destination = self.root / "nul-path-extract"

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsafe archive member path: embedded NUL",
        ):
            MODULE.safe_extract_archive(archive_path, destination)

        self.assertFalse(destination.exists())

    def test_safe_extract_rejects_member_path_limits_before_writing(self) -> None:
        wide_component = "\N{LATIN SMALL LETTER E WITH ACUTE}" * 120
        cases = (
            (
                "path-bytes",
                "root/" + "/".join([wide_component] * 18),
                "path exceeds UTF-8 byte limit",
            ),
            (
                "component-bytes",
                "root/" + "\N{CJK UNIFIED IDEOGRAPH-754C}" * 86,
                "component exceeds UTF-8 byte limit",
            ),
            (
                "depth",
                "/".join(["root", *(["d"] * MODULE.MAX_ARCHIVE_MEMBER_PATH_DEPTH)]),
                "path exceeds depth limit",
            ),
        )
        for name, member_name, expected in cases:
            with self.subTest(limit=name):
                archive_path = self.root / f"path-limit-{name}.tar.gz"
                with tarfile.open(
                    archive_path,
                    "w:gz",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    payload = b"payload"
                    member = tarfile.TarInfo(member_name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                destination = self.root / f"path-limit-{name}-extract"
                with contextlib.ExitStack() as stack:
                    if name == "depth":
                        stack.enter_context(
                            mock.patch.object(
                                MODULE,
                                "PurePosixPath",
                                side_effect=AssertionError(
                                    "path object created before depth validation"
                                ),
                            )
                        )
                    with self.assertRaisesRegex(MODULE.SyncError, expected):
                        MODULE.safe_extract_archive(archive_path, destination)
                self.assertFalse(destination.exists())

    def test_archive_member_path_limits_accept_boundaries_and_shared_prefixes(
        self,
    ) -> None:
        boundary_component = "\N{CJK UNIFIED IDEOGRAPH-754C}" * 85
        boundary_name = f"root/{boundary_component}"
        boundary_member = tarfile.TarInfo(boundary_name)
        with (
            mock.patch.object(
                MODULE,
                "MAX_ARCHIVE_MEMBER_PATH_BYTES",
                len(boundary_name.encode("utf-8")),
            ),
            mock.patch.object(
                MODULE,
                "MAX_ARCHIVE_MEMBER_COMPONENT_BYTES",
                len(boundary_component.encode("utf-8")),
            ),
            mock.patch.object(
                MODULE,
                "MAX_ARCHIVE_MEMBER_PATH_DEPTH",
                len(boundary_name.split("/")),
            ),
        ):
            MODULE._validate_tar_member(boundary_member)
            MODULE._validate_archive_member_paths([boundary_member])

        shared_prefix_members = []
        for index in range(512):
            member = tarfile.TarInfo(
                f"personal-codex/shared/prefix/file-{index:04d}.txt"
            )
            MODULE._validate_tar_member(member)
            shared_prefix_members.append(member)
        MODULE._validate_archive_member_paths(shared_prefix_members)
        self.assertEqual(len(shared_prefix_members), 512)

    def test_archive_member_paths_bound_implicit_directories(self) -> None:
        members = [
            tarfile.TarInfo("root/personal_codex/sync-manifest.json"),
            tarfile.TarInfo("root/a/b/c/file.txt"),
        ]

        with mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBERS", 7):
            MODULE._validate_archive_member_paths(members)
        with (
            mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBERS", 6),
            self.assertRaisesRegex(MODULE.SyncError, "path entry limit"),
        ):
            MODULE._validate_archive_member_paths(members)

    def test_safe_extract_rejects_implicit_directory_limit_before_writing(
        self,
    ) -> None:
        archive_path = self.root / "implicit-directory-limit.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for member_name in (
                "root/personal_codex/sync-manifest.json",
                "root/a/b/c/file.txt",
            ):
                payload = b"x"
                member = tarfile.TarInfo(member_name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        destination = self.root / "implicit-directory-limit-extract"

        with (
            mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBERS", 6),
            mock.patch.object(
                MODULE,
                "_create_archive_destination",
                side_effect=AssertionError("destination must not be created"),
            ) as create_destination,
            self.assertRaisesRegex(MODULE.SyncError, "path entry limit"),
        ):
            MODULE.safe_extract_archive(archive_path, destination)

        create_destination.assert_not_called()
        self.assertFalse(destination.exists())

    def test_validate_tar_member_allows_one_directory_trailing_slash(self) -> None:
        member = tarfile.TarInfo("personal-codex/")
        member.type = tarfile.DIRTYPE

        MODULE._validate_tar_member(member)

        self.assertEqual(member.name, "personal-codex")

    def test_safe_extract_rejects_exact_duplicate_before_writing(self) -> None:
        archive_path = self.root / "duplicate.tar.gz"
        member_name = "personal-codex/payload.txt"
        with tarfile.open(archive_path, "w:gz") as archive:
            for _index in range(2):
                data = b"same"
                member = tarfile.TarInfo(member_name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

        destination = self.root / "duplicate-extract"
        with self.assertRaisesRegex(MODULE.SyncError, "duplicate archive member path"):
            MODULE.safe_extract_archive(archive_path, destination)

        self.assertFalse(destination.exists())

    def test_safe_extract_rejects_portable_path_conflicts_before_writing(
        self,
    ) -> None:
        cases = {
            "case-file": (
                "personal-codex/Foo.txt",
                "personal-codex/foo.txt",
            ),
            "unicode-file": (
                "personal-codex/caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt",
                "personal-codex/cafe\N{COMBINING ACUTE ACCENT}.txt",
            ),
            "case-directory": (
                "personal-codex/Foo/one.txt",
                "personal-codex/foo/two.txt",
            ),
            "unicode-directory": (
                "personal-codex/caf\N{LATIN SMALL LETTER E WITH ACUTE}/one.txt",
                "personal-codex/cafe\N{COMBINING ACUTE ACCENT}/two.txt",
            ),
            "file-directory": (
                "personal-codex/Thing",
                "personal-codex/thing/child.txt",
            ),
        }
        for name, member_names in cases.items():
            with self.subTest(case=name):
                archive_path = self.root / f"portable-{name}.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    for member_name in member_names:
                        data = b"payload"
                        member = tarfile.TarInfo(member_name)
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))

                destination = self.root / f"portable-{name}-extract"
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "portable archive member path conflict",
                ):
                    MODULE.safe_extract_archive(archive_path, destination)

                self.assertFalse(destination.exists())

    def test_safe_extract_rejects_hardlink_member(self) -> None:
        archive_path = self.root / "hardlink.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("personal-codex/link")
            member.type = tarfile.LNKTYPE
            member.linkname = "target"
            archive.addfile(member)

        with self.assertRaisesRegex(MODULE.SyncError, "archive link member"):
            MODULE.safe_extract_archive(archive_path, self.root / "extract")

    def test_safe_extract_rejects_preexisting_symlink_destination(self) -> None:
        source_root = self.root / "preexisting-source"
        write_minimal_release(source_root)
        archive_path = self.root / "preexisting.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        outside = self.root / "outside-extract"
        outside.mkdir()
        destination = self.root / "preexisting-extract"
        destination.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pre-existing archive destination",
        ):
            MODULE.safe_extract_archive(archive_path, destination)

        self.assertTrue(destination.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_safe_extract_destination_swap_does_not_write_redirected_tree(self) -> None:
        source_root = self.root / "destination-swap-source"
        write_minimal_release(source_root)
        archive_path = self.root / "destination-swap.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        destination = self.root / "destination-swap-extract"
        moved_destination = self.root / "destination-swap-bound"
        redirected = self.root / "destination-swap-redirected"
        redirected.mkdir()
        sentinel = redirected / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        original_extract = MODULE._extract_archive_members

        def swap_destination(archive, destination_fd, members):
            destination.rename(moved_destination)
            destination.symlink_to(redirected, target_is_directory=True)
            return original_extract(archive, destination_fd, members)

        with mock.patch.object(
            MODULE,
            "_extract_archive_members",
            swap_destination,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "destination changed"):
                MODULE.safe_extract_archive(archive_path, destination)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(list(redirected.iterdir()), [sentinel])
        self.assertTrue(
            (
                moved_destination
                / f"personal-codex-{SHA1}"
                / "personal_codex"
                / "sync-manifest.json"
            ).is_file()
        )

    def test_safe_extract_parent_swap_does_not_create_in_redirected_parent(self) -> None:
        source_root = self.root / "parent-swap-source"
        write_minimal_release(source_root)
        archive_path = self.root / "parent-swap.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        parent = self.root / "parent-swap-parent"
        parent.mkdir()
        moved_parent = self.root / "parent-swap-bound"
        redirected = self.root / "parent-swap-redirected"
        redirected.mkdir()
        sentinel = redirected / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        destination = parent / "extract"
        original_create_directory = MODULE._create_archive_directory_at
        swapped = False

        def swap_parent(parent_fd, name):
            nonlocal swapped
            if not swapped:
                swapped = True
                parent.rename(moved_parent)
                parent.symlink_to(redirected, target_is_directory=True)
            return original_create_directory(parent_fd, name)

        with mock.patch.object(
            MODULE,
            "_create_archive_directory_at",
            swap_parent,
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "parent changed"):
                MODULE.safe_extract_archive(archive_path, destination)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(list(redirected.iterdir()), [sentinel])
        self.assertTrue((moved_parent / "extract").is_dir())

    def test_safe_extract_preserves_concurrent_expected_leaves(self) -> None:
        source_root = self.root / "leaf-race-source"
        write_minimal_release(source_root)
        archive_path = self.root / "leaf-race.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        original_rename = MODULE._rename_noreplace_at
        for kind in ("regular", "symlink", "directory"):
            with self.subTest(kind=kind):
                destination = self.root / f"leaf-race-{kind}-extract"
                outside = self.root / f"leaf-race-{kind}-outside.txt"
                outside.write_text("outside\n", encoding="utf-8")
                inserted = False

                def insert_expected_leaf(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                ):
                    nonlocal inserted
                    if destination_name == "sync-manifest.json" and not inserted:
                        inserted = True
                        if kind == "regular":
                            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                            existing_fd = os.open(
                                destination_name,
                                flags,
                                0o600,
                                dir_fd=destination_parent_fd,
                            )
                            try:
                                os.write(existing_fd, b"concurrent\n")
                            finally:
                                os.close(existing_fd)
                        elif kind == "symlink":
                            os.symlink(
                                outside,
                                destination_name,
                                dir_fd=destination_parent_fd,
                            )
                        else:
                            os.mkdir(destination_name, dir_fd=destination_parent_fd)
                    return original_rename(
                        source_parent_fd,
                        source_name,
                        destination_parent_fd,
                        destination_name,
                    )

                with mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    insert_expected_leaf,
                ):
                    with self.assertRaisesRegex(MODULE.SyncError, "entry already exists"):
                        MODULE.safe_extract_archive(archive_path, destination)

                existing = (
                    destination
                    / f"personal-codex-{SHA1}"
                    / "personal_codex"
                    / "sync-manifest.json"
                )
                self.assertTrue(inserted)
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
                if kind == "regular":
                    self.assertEqual(
                        existing.read_text(encoding="utf-8"),
                        "concurrent\n",
                    )
                elif kind == "symlink":
                    self.assertTrue(existing.is_symlink())
                    self.assertEqual(os.readlink(existing), str(outside))
                else:
                    self.assertTrue(existing.is_dir())
                    self.assertEqual(list(existing.iterdir()), [])

    def test_safe_extract_succeeds_into_new_destination(self) -> None:
        source_root = self.root / "success-source"
        write_minimal_release(source_root)
        archive_path = self.root / "success.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")

        destination = self.root / "success-extract"
        release_root = MODULE.safe_extract_archive(archive_path, destination)

        self.assertEqual(release_root, destination / f"personal-codex-{SHA1}")
        self.assertTrue(
            (release_root / "personal_codex" / "sync-manifest.json").is_file()
        )
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_safe_extract_does_not_retain_one_fd_per_directory(self) -> None:
        source_root = self.root / "many-directories-source"
        write_minimal_release(source_root)
        for index in range(300):
            directory = source_root / "extras" / f"item-{index:03d}"
            directory.mkdir(parents=True)
        archive_path = self.root / "many-directories.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")

        real_create = MODULE._create_archive_directory_at
        created_identities: dict[int, tuple[int, int]] = {}
        peak_live_created_fds = 0

        def tracked_create(parent_fd, name):
            nonlocal peak_live_created_fds
            directory_fd = real_create(parent_fd, name)
            metadata = os.fstat(directory_fd)
            created_identities[directory_fd] = (metadata.st_dev, metadata.st_ino)
            live_created_fds = 0
            for candidate_fd, expected_identity in created_identities.items():
                try:
                    candidate_metadata = os.fstat(candidate_fd)
                except OSError:
                    continue
                if (
                    candidate_metadata.st_dev,
                    candidate_metadata.st_ino,
                ) == expected_identity:
                    live_created_fds += 1
            peak_live_created_fds = max(
                peak_live_created_fds,
                live_created_fds,
            )
            return directory_fd

        with mock.patch.object(
            MODULE,
            "_create_archive_directory_at",
            side_effect=tracked_create,
        ):
            release_root = MODULE.safe_extract_archive(
                archive_path,
                self.root / "many-directories-extract",
            )

        self.assertTrue((release_root / "extras" / "item-299").is_dir())
        self.assertLessEqual(peak_live_created_fds, 3)

    def test_safe_extract_reopen_rejects_replaced_directory_identity(self) -> None:
        source_root = self.root / "reopen-identity-source"
        write_minimal_release(source_root)
        protected = source_root / "extras" / "protected"
        protected.mkdir(parents=True)
        (protected / "payload.txt").write_text("trusted\n", encoding="utf-8")
        archive_path = self.root / "reopen-identity.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")

        destination = self.root / "reopen-identity-extract"
        extracted = destination / f"personal-codex-{SHA1}" / "extras" / "protected"
        moved = self.root / "reopen-identity-bound"
        real_validate = MODULE._validate_archive_tree_evidence
        swapped = False

        def replace_directory_before_validation(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                extracted.rename(moved)
                extracted.mkdir()
                (extracted / "sentinel.txt").write_text(
                    "replacement\n",
                    encoding="utf-8",
                )
            return real_validate(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_validate_archive_tree_evidence",
            side_effect=replace_directory_before_validation,
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "archive directory changed",
            ):
                MODULE.safe_extract_archive(archive_path, destination)

        self.assertTrue(swapped)
        self.assertEqual(
            (extracted / "sentinel.txt").read_text(encoding="utf-8"),
            "replacement\n",
        )
        self.assertEqual(
            (moved / "payload.txt").read_text(encoding="utf-8"),
            "trusted\n",
        )

    def test_safe_extract_descriptor_writer_sanitizes_member_modes(self) -> None:
        source_root = self.root / "source"
        write_minimal_release(source_root)
        executable = source_root / "personal_codex" / "bin" / "example-tool"
        executable.chmod(0o6777)
        archive_path = self.root / "release.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        with mock.patch.object(
            tarfile.TarFile,
            "extractall",
            side_effect=AssertionError("extractall must not be used"),
        ) as extractall:
            release_root = MODULE.safe_extract_archive(archive_path, self.root / "extract")

        extractall.assert_not_called()
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

    def test_load_manifest_translates_excessive_json_depth(self) -> None:
        release_root = self.root / "deep-manifest"
        write_minimal_release(release_root)

        with mock.patch.object(
            MODULE.json,
            "loads",
            side_effect=RecursionError("maximum JSON depth exceeded"),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "Invalid JSON"):
                MODULE.load_manifest_data(release_root)

    def test_load_manifest_translates_oversized_json_integer(self) -> None:
        release_root = self.root / "large-integer-manifest"
        write_minimal_release(release_root)
        manifest_path = release_root / MODULE.MANIFEST_RELATIVE_PATH
        manifest_path.write_text(
            '{"version":' + "9" * 10_000 + ',"links":[]}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MODULE.SyncError, "Invalid JSON"):
            MODULE.load_manifest_data(release_root)

    def test_manifest_active_link_limit_reserves_current_wal_record(self) -> None:
        self.assertEqual(
            MODULE.MAX_MANIFEST_ACTIVE_LINKS,
            min(
                MODULE.MAX_PENDING_LINK_RECORDS,
                MODULE.MAX_PENDING_LINK_CLAIMS,
            )
            - 1,
        )
        data = {
            "version": 1,
            "links": [
                {
                    "source": "personal_codex/AGENTS.md",
                    "target": "AGENTS.md",
                    "kind": "file",
                },
                {
                    "source": "personal_codex/AGENTS.md",
                    "target": "bin/example",
                    "kind": "file",
                },
            ],
        }

        with mock.patch.object(MODULE, "MAX_MANIFEST_ACTIVE_LINKS", 1):
            with self.assertRaisesRegex(MODULE.SyncError, "transaction limit"):
                MODULE._parse_manifest_data(data, lambda _path: "file")

    def test_manifest_target_paths_enforce_byte_and_depth_limits(self) -> None:
        boundary = "/".join(["é" * 31] * 63 + ["é" * 63 + "x"])
        byte_overflow = boundary + "x"
        component_boundary = "é" * 127 + "x"
        component_overflow = component_boundary + "x"
        depth_overflow = "/".join(
            "x" for _ in range(MODULE.MAX_MANIFEST_TARGET_PATH_DEPTH + 1)
        )
        self.assertEqual(
            len(boundary.encode("utf-8")),
            MODULE.MAX_MANIFEST_TARGET_PATH_BYTES,
        )
        self.assertEqual(
            len(Path(boundary).parts),
            MODULE.MAX_MANIFEST_TARGET_PATH_DEPTH,
        )
        self.assertEqual(
            len(component_boundary.encode("utf-8")),
            MODULE.MAX_MANIFEST_TARGET_COMPONENT_BYTES,
        )

        def payload(route: str, target: str) -> dict[str, object]:
            data: dict[str, object] = {
                "version": 1,
                "links": [
                    {
                        "source": "personal_codex/AGENTS.md",
                        "target": "AGENTS.md",
                        "kind": "file",
                    }
                ],
            }
            if route == "active":
                data["links"][0]["target"] = target  # type: ignore[index]
                return data
            removed_link: dict[str, object] = {
                "id": "retired",
                "source": "personal_codex/retired",
                "target": "skills/retired",
                "kind": "file",
            }
            if route == "removed":
                removed_link["target"] = target
            else:
                removed_link["replacement_target"] = target
            data["removed_links"] = [removed_link]
            return data

        for route in ("active", "removed", "replacement"):
            with self.subTest(route=route, limit="boundary"):
                MODULE._parse_manifest_data(
                    payload(route, boundary),
                    lambda _path: "file",
                )
            with self.subTest(route=route, limit="bytes"):
                with self.assertRaisesRegex(MODULE.SyncError, "UTF-8 bytes"):
                    MODULE._parse_manifest_data(
                        payload(route, byte_overflow),
                        lambda _path: "file",
                    )
            with self.subTest(route=route, limit="component-boundary"):
                MODULE._parse_manifest_data(
                    payload(route, component_boundary),
                    lambda _path: "file",
                )
            with self.subTest(route=route, limit="component"):
                with self.assertRaisesRegex(MODULE.SyncError, "component 1"):
                    MODULE._parse_manifest_data(
                        payload(route, component_overflow),
                        lambda _path: "file",
                    )
            with self.subTest(route=route, limit="depth"):
                with self.assertRaisesRegex(MODULE.SyncError, "path components"):
                    MODULE._parse_manifest_data(
                        payload(route, depth_overflow),
                        lambda _path: "file",
                    )

        for label, target, message in (
            ("bytes", byte_overflow, "UTF-8 bytes"),
            ("component", component_overflow, "component 1"),
            ("depth", depth_overflow, "path components"),
        ):
            with (
                self.subTest(early_rejection=label),
                mock.patch.object(MODULE, "_portable_target_key") as portable_key,
                self.assertRaisesRegex(MODULE.SyncError, message),
            ):
                MODULE._validate_target_path(target, "target")
            portable_key.assert_not_called()

    def test_base_release_repo_requires_owner_repository_form(self) -> None:
        data = {
            "version": 1,
            "links": [
                {
                    "source": "personal_codex/AGENTS.md",
                    "target": "AGENTS.md",
                    "kind": "file",
                }
            ],
        }
        invalid_repositories = (
            "",
            "owner",
            "owner/repo/extra",
            "/",
            "/repo",
            "owner/",
            ".owner/repo",
            "owner/.repo",
            1,
            True,
            [],
        )

        for repository in invalid_repositories:
            with self.subTest(repository=repository):
                invalid_data = dict(data)
                invalid_data["base_release"] = {"repo": repository}
                with self.assertRaisesRegex(MODULE.SyncError, "owner/repo string"):
                    MODULE._parse_manifest_data(invalid_data, lambda _path: "file")

        manifest = MODULE._parse_manifest_data(data, lambda _path: "file")
        for fallback in invalid_repositories:
            with self.subTest(fallback=fallback):
                with self.assertRaisesRegex(MODULE.SyncError, "owner/repo string"):
                    MODULE._load_base_release_spec(manifest, fallback)

    def test_manifest_payload_digest_translates_serialization_errors(self) -> None:
        data = {
            "version": 1,
            "links": [
                {
                    "source": "personal_codex/AGENTS.md",
                    "target": "AGENTS.md",
                    "kind": "file",
                }
            ],
        }

        for error in (
            ValueError("circular reference"),
            RecursionError("maximum recursion depth exceeded"),
            TypeError("unsupported value"),
            OverflowError("value too large"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(MODULE.json, "dumps", side_effect=error):
                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "could not be canonicalized",
                    ):
                        MODULE._parse_manifest_data(data, lambda _path: "file")

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

    def test_load_manifest_rejects_sync_internal_target(self) -> None:
        release_root = self.root / "release"
        write_minimal_release(release_root)
        manifest_path = release_root / "personal_codex" / "sync-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["links"][0]["target"] = "personal-sync/state/managed-links.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MODULE.SyncError, "sync internal path"):
            MODULE.load_manifest(release_root)

    def test_load_manifest_rejects_current_directory_source(self) -> None:
        release_root = self.root / "release"
        write_minimal_release(release_root)
        manifest_path = release_root / "personal_codex" / "sync-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["links"][0]["source"] = "."
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(MODULE.SyncError, "current-dir segments"):
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

    def test_load_manifest_accepts_removed_links(self) -> None:
        release_root = self.root / "release"
        write_skill_manifest_release(
            release_root,
            removed_links=[
                {
                    "id": "remove-old-skill",
                    "source": "personal_codex/skills/old-skill",
                    "target": "skills/old-skill",
                    "kind": "skill",
                    "legacy": True,
                }
            ],
        )

        manifest = MODULE.load_manifest_data(release_root)

        self.assertEqual(len(manifest.removed_links), 1)
        self.assertEqual(manifest.removed_links[0].id, "remove-old-skill")
        self.assertTrue(manifest.removed_links[0].legacy)

    def test_load_manifest_rejects_unsafe_removed_link_path(self) -> None:
        release_root = self.root / "release"
        write_skill_manifest_release(
            release_root,
            removed_links=[
                {
                    "id": "remove-old-skill",
                    "source": "../old-skill",
                    "target": "skills/old-skill",
                    "kind": "skill",
                }
            ],
        )

        with self.assertRaisesRegex(MODULE.SyncError, "parent traversal"):
            MODULE.load_manifest_data(release_root)

    def test_load_manifest_rejects_duplicate_removed_link_id(self) -> None:
        release_root = self.root / "release"
        removed = {
            "id": "remove-old-skill",
            "source": "personal_codex/skills/old-skill",
            "target": "skills/old-skill",
            "kind": "skill",
        }
        write_skill_manifest_release(
            release_root,
            removed_links=[removed, dict(removed)],
        )

        with self.assertRaisesRegex(MODULE.SyncError, "duplicate removed link id"):
            MODULE.load_manifest_data(release_root)

    def test_load_manifest_rejects_ancestor_target_collision(self) -> None:
        release_root = self.root / "release"
        write_skill_manifest_release(release_root, skills=("parent", "child"))
        manifest_path = release_root / "personal_codex" / "sync-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["links"][1]["target"] = "skills/parent/child"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(MODULE.SyncError, "must not overlap"):
            MODULE.load_manifest_data(release_root)

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

        self.run_quietly(
            MODULE.install_release_tree,
            release_root,
            home,
            SHA1,
            dry_run=False,
        )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "AGENTS.md").is_symlink())
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "agent\n")
        self.assertTrue((home / "skills" / "example-skill").is_symlink())
        self.assertTrue((home / "bin" / "example-tool").is_symlink())
        self.assertTrue((home / "bin" / "codex-personal-sync").is_symlink())
        self.assertTrue((home / "skills" / ".system").is_dir())
        self.assertTrue((home / "skills" / "host-local").is_dir())
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertEqual(
            {entry["target"] for entry in state["links"]},
            {
                "AGENTS.md",
                "skills/example-skill",
                "bin/example-tool",
                "bin/codex-personal-sync",
            },
        )

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

        self.run_quietly(
            MODULE.install_release_tree,
            release_root,
            home,
            SHA1,
            dry_run=False,
        )

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
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([entry["target"] for entry in state["links"]], ["AGENTS.md"])

    def test_install_release_tree_rejects_interrupted_current_switch(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        release_two_dir = home / "personal-sync" / "releases" / SHA2
        shutil.copytree(release_two, release_two_dir)
        self.run_quietly(MODULE._switch_current, home, SHA2, dry_run=False)
        before = snapshot_tree(home)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed state/current release mismatch",
        ):
            self.run_quietly(
                MODULE.install_release_tree,
                release_two,
                home,
                SHA2,
                dry_run=False,
            )

        self.assertEqual(snapshot_tree(home), before)
        self.assertEqual(current_target(home), f"releases/{SHA2}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "two\n")
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(home))
        )

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

    def test_install_release_tree_rejects_symlink_parent(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        external_skills = self.root / "external-skills"
        write_minimal_release(release_root)
        home.mkdir(parents=True)
        external_skills.mkdir()
        (home / "skills").symlink_to(external_skills, target_is_directory=True)

        with self.assertRaisesRegex(MODULE.SyncError, "below symlink parent"):
            self.run_quietly(
                MODULE.install_release_tree,
                release_root,
                home,
                SHA1,
                dry_run=False,
            )

        self.assertEqual(list(external_skills.iterdir()), [])
        self.assertFalse((home / "personal-sync").exists())

    def test_install_release_tree_rejects_parent_link_to_child_migration(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_skill_manifest_release(release_one, skills=("bundle",))
        first_manifest_path = release_one / "personal_codex" / "sync-manifest.json"
        first_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
        first_manifest["links"][0]["target"] = "skills"
        first_manifest_path.write_text(json.dumps(first_manifest), encoding="utf-8")
        (release_one / "personal_codex" / "skills" / "bundle" / "nested").mkdir()
        write_skill_manifest_release(
            release_two,
            skills=("nested",),
            removed_links=[
                {
                    "id": "move-skills-parent-to-child",
                    "source": "personal_codex/skills/bundle",
                    "target": "skills",
                    "kind": "skill",
                }
            ],
        )
        self.run_quietly(
            MODULE.install_release_tree,
            release_one,
            home,
            SHA1,
            dry_run=False,
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        old_state = state_path.read_bytes()

        with self.assertRaisesRegex(MODULE.SyncError, "manifest targets must not overlap"):
            self.run_quietly(
                MODULE.install_release_tree,
                release_two,
                home,
                SHA2,
                dry_run=False,
            )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "skills").is_symlink())
        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertFalse((home / "personal-sync" / "releases" / SHA2).exists())

    def test_install_release_tree_rejects_child_link_to_parent_migration(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_skill_manifest_release(release_one, skills=("nested",))
        write_skill_manifest_release(
            release_two,
            skills=("bundle",),
            removed_links=[
                {
                    "id": "move-skills-child-to-parent",
                    "source": "personal_codex/skills/nested",
                    "target": "skills/nested",
                    "kind": "skill",
                }
            ],
        )
        second_manifest_path = release_two / "personal_codex" / "sync-manifest.json"
        second_manifest = json.loads(second_manifest_path.read_text(encoding="utf-8"))
        second_manifest["links"][0]["target"] = "skills"
        second_manifest_path.write_text(json.dumps(second_manifest), encoding="utf-8")
        self.run_quietly(
            MODULE.install_release_tree,
            release_one,
            home,
            SHA1,
            dry_run=False,
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        old_state = state_path.read_bytes()

        with self.assertRaisesRegex(MODULE.SyncError, "manifest targets must not overlap"):
            self.run_quietly(
                MODULE.install_release_tree,
                release_two,
                home,
                SHA2,
                dry_run=False,
            )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "skills" / "nested").is_symlink())
        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertFalse((home / "personal-sync" / "releases" / SHA2).exists())

        fresh_home = self.root / "fresh-home" / ".codex"
        self.run_quietly(
            MODULE.install_release_tree,
            release_two,
            fresh_home,
            SHA2,
            dry_run=False,
        )
        self.run_quietly(
            MODULE.install_release_tree,
            release_two,
            fresh_home,
            SHA2,
            dry_run=False,
        )
        self.assertTrue((fresh_home / "skills").is_symlink())
        fresh_state = json.loads(
            (
                fresh_home / "personal-sync" / "state" / "managed-links.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [entry["target"] for entry in fresh_state["links"]],
            ["skills"],
        )

    def test_install_release_tree_rejects_tampered_managed_state(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["links"][0]["link_target"] = "../local-file"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(MODULE.SyncError, "unexpected link_target"):
            self.run_quietly(
                MODULE.install_release_tree,
                release_root,
                home,
                SHA1,
                dry_run=False,
            )

    def test_install_release_tree_bootstraps_historical_manifest_ownership(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one)
        write_agent_only_release(release_two)
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        state_path.unlink()
        historical_link = home / "skills" / "example-skill"
        historical_link.symlink_to(
            "../personal-sync/current/personal_codex/skills/example-skill",
            target_is_directory=True,
        )

        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)

        self.assertFalse(os.path.lexists(historical_link))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual([entry["target"] for entry in state["links"]], ["AGENTS.md"])

    def test_install_release_tree_replaces_same_owner_source_migration(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_skill_manifest_release(
            release_root,
            skills=("new-source",),
            extra_skill_dirs=("old-source",),
            removed_links=[
                {
                    "id": "rename-old-source",
                    "source": "personal_codex/skills/old-source",
                    "target": "skills/stable",
                    "kind": "skill",
                    "legacy": True,
                }
            ],
        )
        manifest_path = release_root / "personal_codex" / "sync-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["links"][0]["target"] = "skills/stable"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        old_link = home / "skills" / "stable"
        old_link.parent.mkdir(parents=True)
        old_link.symlink_to(
            "../personal-sync/current/personal_codex/skills/old-source",
            target_is_directory=True,
        )

        with self.capture_reconcile_backups() as backup_events:
            self.run_quietly(
                MODULE.install_release_tree,
                release_root,
                home,
                SHA1,
                dry_run=False,
            )

        self.assertEqual(
            old_link.readlink().as_posix(),
            "../personal-sync/current/personal_codex/skills/new-source",
        )
        self.assertEqual(
            list(
                (home / "personal-sync" / "quarantine").glob(
                    "*/links/skills/stable"
                )
            ),
            [],
        )
        self.assertIn(
            (
                "quarantine-replace",
                "skills/stable",
                "../personal-sync/current/personal_codex/skills/old-source",
                "public:rename-old-source",
            ),
            backup_events,
        )

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
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
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
                        archive_id=1,
                        archive_size=1,
                        checksum_id=2,
                        checksum_size=1,
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
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertEqual(
            {entry["release_sha"] for entry in state["links"]},
            {SHA1},
        )

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

    def test_rollback_rejects_interrupted_current_switch(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(MODULE.install_release_tree, release_one, home, SHA1, dry_run=False)
        self.run_quietly(MODULE.install_release_tree, release_two, home, SHA2, dry_run=False)
        self.run_quietly(MODULE._switch_current, home, SHA1, dry_run=False)
        before = snapshot_tree(home)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed state/current release mismatch",
        ):
            self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(snapshot_tree(home), before)
        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "one\n")
        state = json.loads(
            (home / "personal-sync" / "state" / "managed-links.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["owners"], {"public": SHA2})
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(home))
        )

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

    def test_rollback_to_current_release_repairs_missing_current_pointer(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        current = MODULE._current_link(home)
        current.unlink()

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
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
        with self.assertRaises(MODULE.SyncError):
            MODULE.status(home)

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
                            {
                                "id": 101,
                                "name": f"personal-codex-{SHA1}.tar.gz",
                                "size": 1,
                            },
                            {
                                "id": 102,
                                "name": f"personal-codex-{SHA1}.sha256",
                                "size": 1,
                            },
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

    def test_current_sha_rejects_absolute_current_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(MODULE.install_release_tree, release_root, home, SHA1, dry_run=False)
        current = home / "personal-sync" / "current"
        current.unlink()
        current.symlink_to(home / "personal-sync" / "releases" / SHA1, target_is_directory=True)

        with self.assertRaisesRegex(MODULE.SyncError, "must use releases/<sha>"):
            MODULE._current_sha(home)

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
                {"id": 101, "name": archive_name, "size": archive_path.stat().st_size},
                {"id": 102, "name": checksum_name, "size": checksum_path.stat().st_size},
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
                {"id": 101, "name": archive_name, "size": archive_path.stat().st_size},
                {"id": 102, "name": checksum_name, "size": checksum_path.stat().st_size},
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
