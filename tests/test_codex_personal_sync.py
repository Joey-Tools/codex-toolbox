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
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
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


def github_sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def github_release_asset(
    asset_id: object,
    name: str,
    *,
    size: object = 1,
    state: object = "uploaded",
    digest: object = "sha256:" + "0" * 64,
) -> dict[str, object]:
    return {
        "id": asset_id,
        "name": name,
        "size": size,
        "state": state,
        "digest": digest,
    }


class FakeDownloadProcess:
    def __new__(
        cls,
        payload: bytes,
        *,
        returncode: int = 0,
    ) -> MODULE._GuardedProcess:
        del cls
        return MODULE._spawn_guarded_process(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys;"
                    "os.write(1,bytes.fromhex(sys.argv[1]));"
                    "raise SystemExit(int(sys.argv[2]))"
                ),
                payload.hex(),
                str(returncode),
            ],
            deadline=time.monotonic() + 5.0,
            process_label="fake download",
            unavailable_code="test-unavailable",
            unavailable_message="test executable unavailable",
        )


class CloseFailingSelector:
    def __init__(self, inner, error: BaseException) -> None:
        self.inner = inner
        self.error = error

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self) -> None:
        self.inner.close()
        raise self.error


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


def write_agent_only_release(
    release_root: Path, *, agent_text: str = "agent\n"
) -> None:
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


def write_reference_only_agent_release(
    release_root: Path,
    *,
    agent_text: str = "agent\n",
) -> None:
    write_skill_manifest_release(
        release_root,
        skills=("reference-only-base",),
    )
    personal_root = release_root / "personal_codex"
    (personal_root / "AGENTS.md").write_text(agent_text, encoding="utf-8")
    manifest_path = personal_root / "sync-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reference_only"] = ["personal_codex/AGENTS.md"]
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
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


def foreign_leaf_snapshot(path: Path) -> tuple[object, ...]:
    metadata = path.lstat()
    identity = (metadata.st_dev, metadata.st_ino)
    if stat.S_ISLNK(metadata.st_mode):
        return ("symlink", identity, path.readlink().as_posix())
    if stat.S_ISREG(metadata.st_mode):
        return ("file", identity, path.read_bytes())
    return ("other", identity, stat.S_IFMT(metadata.st_mode))


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
        self.archive_workspace_context = MODULE.bind_archive_workspace(self.root)
        self.archive_workspace = self.archive_workspace_context.__enter__()
        self.user_home = self.root / "home"
        self.path_home_patch = mock.patch.object(
            MODULE.Path, "home", return_value=self.user_home
        )
        self.path_home_patch.start()

    def tearDown(self) -> None:
        self.path_home_patch.stop()
        self.archive_workspace_context.__exit__(None, None, None)
        self.tmpdir.cleanup()

    def safe_extract_archive(self, archive_path: Path, destination: Path) -> Path:
        return MODULE.safe_extract_archive(
            archive_path,
            destination,
            workspace=self.archive_workspace,
        )

    def verify_and_extract_archive(
        self,
        archive_path: Path,
        checksum_path: Path,
        destination: Path,
    ):
        return MODULE.verify_and_extract_archive(
            archive_path,
            checksum_path,
            destination,
            workspace=self.archive_workspace,
        )

    def download_release_assets(
        self,
        repo: str,
        assets: MODULE.ReleaseAssets,
        destination: Path,
    ) -> None:
        MODULE.download_release_assets(
            repo,
            assets,
            destination,
            workspace=self.archive_workspace,
        )

    def download_and_extract_release(
        self,
        repo: str,
        destination: Path,
        *,
        sha: str | None = None,
    ) -> MODULE.DownloadedRelease:
        return MODULE.download_and_extract_release(
            repo,
            destination,
            workspace=self.archive_workspace,
            sha=sha,
        )

    def run_quietly(self, callback, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return callback(*args, **kwargs)

    def snapshot_release_tree(self, release_root: Path):
        release_fd = os.open(release_root, MODULE._source_directory_flags())
        try:
            return MODULE._release_tree_snapshot_from_directory_fd(
                release_fd,
                release_root,
                require_sanitized_modes=False,
                capture_limits={},
            )
        finally:
            os.close(release_fd)

    @contextlib.contextmanager
    def capture_reconcile_backups(self):
        events: list[tuple[str, str, str | None, str | None]] = []
        real_verify = MODULE._verify_reconcile_backup

        def capture(home: Path, action, backup: Path) -> None:
            real_verify(home, action, backup)
            relative_target = action.target.relative_to(home)
            relative_backup = backup.relative_to(home / "personal-sync" / "quarantine")
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
        def fake_download(
            repo: str,
            destination: Path,
            *,
            workspace,
            sha: str | None = None,
        ):
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
        scheduler_args = parser.parse_args(["install-scheduler"])
        self.assertIsNone(scheduler_args.repo)
        self.assertIsNone(scheduler_args.mode)
        self.assertIsNone(scheduler_args.base_repo)
        self.assertIsNone(scheduler_args.owner)

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
        self.assertIsNone(scheduler_args.repo)

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
        scheduler_defaults = parser.parse_args(["install-scheduler"])
        self.assertIsNone(scheduler_defaults.repo)

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
        self.assertIsNone(scheduler_args.base_repo)

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
        self.assertIsNone(scheduler_args.base_repo)

    def test_install_private_downloads_public_base_and_overlay(self) -> None:
        public_release = self.root / "public-release"
        private_release = self.root / "private-release"
        home = self.root / "home" / ".codex"
        write_minimal_release(public_release, agent_text="public\n")
        write_private_skill_only_release(private_release)
        downloads: list[tuple[str, str | None]] = []

        def fake_download(
            repo: str,
            destination: Path,
            *,
            workspace,
            sha: str | None = None,
        ):
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

        def fake_download(
            repo: str,
            destination: Path,
            *,
            workspace,
            sha: str | None = None,
        ):
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
            entry
            for entry in state["links"]
            if entry["target"] == "skills/moving-skill"
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
            (home / "personal-sync" / "overlays" / "private" / "current")
            .readlink()
            .as_posix(),
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
                    "retires_replacements": ["private:move-moving-skill-to-public"],
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
                    "retires_replacements": ["private:move-moving-skill-to-public"],
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
                    "retires_replacements": ["private:move-moving-skill-to-public"],
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

    def test_install_private_rejects_unclaimed_legacy_tombstone_replacement(
        self,
    ) -> None:
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
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        legacy_metadata = legacy_link.lstat()
        before = (
            legacy_metadata.st_dev,
            legacy_metadata.st_ino,
            legacy_link.readlink().as_posix(),
            current_target(home),
            (home / "personal-sync" / "overlays" / "private" / "current")
            .readlink()
            .as_posix(),
            state_path.read_bytes(),
        )

        with self.capture_reconcile_backups() as backup_events:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "refusing to replace unproven symlink target",
            ):
                self.install_private_pair(
                    home,
                    new_public,
                    new_private,
                    public_sha=SHA3,
                    private_sha=SHA4,
                )

        legacy_metadata = legacy_link.lstat()
        self.assertEqual(
            (
                legacy_metadata.st_dev,
                legacy_metadata.st_ino,
                legacy_link.readlink().as_posix(),
                current_target(home),
                (home / "personal-sync" / "overlays" / "private" / "current")
                .readlink()
                .as_posix(),
                state_path.read_bytes(),
            ),
            before,
        )
        self.assertNotIn(
            "skills/legacy-skill",
            {event[1] for event in backup_events},
        )
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))
        self.assertFalse((MODULE._releases_root(home) / SHA3).exists())
        self.assertFalse((MODULE._releases_root(home, "private") / SHA4).exists())

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

        self.assertEqual(
            (local_directory / "local.txt").read_text(encoding="utf-8"), "local\n"
        )
        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(snapshot_tree(quarantine_root), quarantine_before)

    def test_install_private_does_not_commit_state_after_overlay_verification_failure(
        self,
    ) -> None:
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

        with mock.patch.object(
            MODULE, "_collect_overlay_issues", return_value=["forced"]
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError, "overlay verification failed"
            ):
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

    def test_install_private_rejects_cross_layer_ancestor_target_collision(
        self,
    ) -> None:
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

    def test_install_private_does_not_reapply_tombstone_cleanup_with_existing_ledger(
        self,
    ) -> None:
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
        legacy_metadata = legacy_link.lstat()
        legacy_before = (
            legacy_metadata.st_dev,
            legacy_metadata.st_ino,
            legacy_link.readlink().as_posix(),
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        state_before = state_path.read_bytes()

        with self.capture_reconcile_backups() as first_backup_events:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "overlay verification failed with 1 issue",
            ):
                self.install_private_pair(
                    home,
                    new_public,
                    new_private,
                    public_sha=SHA3,
                    private_sha=SHA4,
                )
        legacy_metadata = legacy_link.lstat()
        self.assertEqual(
            (
                legacy_metadata.st_dev,
                legacy_metadata.st_ino,
                legacy_link.readlink().as_posix(),
            ),
            legacy_before,
        )
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(
            list(
                (home / "personal-sync" / "quarantine").glob(
                    "*/links/skills/legacy-skill"
                )
            ),
            [],
        )
        self.assertNotIn(
            "skills/legacy-skill",
            {event[1] for event in first_backup_events},
        )
        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(
            (home / "personal-sync" / "overlays" / "private" / "current")
            .readlink()
            .as_posix(),
            f"releases/{SHA2}",
        )
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_install_private_legacy_tombstone_dry_run_rejects_without_side_effects(
        self,
    ) -> None:
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

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "refusing to replace unproven symlink target",
        ):
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
                home / "personal-sync" / "overlays" / "private" / "releases" / SHA4
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
            entry for entry in manifest["links"] if entry["target"] == "skills/shared"
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

    def test_uninstall_overlay_relinquishes_foreign_agents_claim(self) -> None:
        home = self.root / "uninstall-foreign-agents" / "home" / ".codex"
        public_release = self.root / "uninstall-foreign-agents" / "public"
        private_release = self.root / "uninstall-foreign-agents" / "private"
        write_reference_only_agent_release(public_release)
        write_private_agent_release(private_release)
        self.install_private_pair(
            home,
            public_release,
            private_release,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        agents = home / "AGENTS.md"
        agents.unlink()
        agents.write_text("local\n", encoding="utf-8")
        foreign_before = foreign_leaf_snapshot(agents)

        self.run_quietly(MODULE.uninstall_overlay, home, "private", dry_run=False)

        private_current = home / "personal-sync" / "overlays" / "private" / "current"
        self.assertFalse(os.path.lexists(private_current))
        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(foreign_leaf_snapshot(agents), foreign_before)
        state = json.loads(MODULE._state_path(home).read_text(encoding="utf-8"))
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertNotIn(
            "AGENTS.md",
            {entry["target"] for entry in state["links"]},
        )
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_install_private_rejects_foreign_agents_while_still_mandatory(
        self,
    ) -> None:
        home = self.root / "mandatory-foreign-agents" / "home" / ".codex"
        public_one = self.root / "mandatory-foreign-agents" / "public-one"
        public_two = self.root / "mandatory-foreign-agents" / "public-two"
        private_one = self.root / "mandatory-foreign-agents" / "private-one"
        private_two = self.root / "mandatory-foreign-agents" / "private-two"
        write_reference_only_agent_release(public_one, agent_text="public-one\n")
        write_reference_only_agent_release(public_two, agent_text="public-two\n")
        write_private_agent_release(private_one, agent_text="private-one\n")
        write_private_agent_release(private_two, agent_text="private-two\n")
        self.install_private_pair(
            home,
            public_one,
            private_one,
            public_sha=SHA1,
            private_sha=SHA2,
        )
        agents = home / "AGENTS.md"
        agents.unlink()
        agents.write_text("local\n", encoding="utf-8")
        foreign_before = foreign_leaf_snapshot(agents)
        state_before = MODULE._state_path(home).read_bytes()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "managed state/link target mismatch",
        ):
            self.install_private_pair(
                home,
                public_two,
                private_two,
                public_sha=SHA3,
                private_sha=SHA4,
            )

        self.assertEqual(foreign_leaf_snapshot(agents), foreign_before)
        self.assertEqual(MODULE._state_path(home).read_bytes(), state_before)
        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(
            (home / "personal-sync" / "overlays" / "private" / "current")
            .readlink()
            .as_posix(),
            f"releases/{SHA2}",
        )
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_uninstall_overlay_rolls_back_then_retries_after_write_failure(
        self,
    ) -> None:
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
        private_current = home / "personal-sync" / "overlays" / "private" / "current"
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

    def test_uninstall_overlay_with_missing_current_rolls_back_and_retries(
        self,
    ) -> None:
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
        private_current = home / "personal-sync" / "overlays" / "private" / "current"
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
            if record["scope"] == "current" and record["action"] == "retire-absent"
        )
        retired_payload["owner"] = "ghost"
        retired_payload["target"] = "personal-sync/overlays/ghost/current"
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

    def test_uninstall_overlay_retains_pending_when_outgoing_release_changes(
        self,
    ) -> None:
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
        private_current = home / "personal-sync" / "overlays" / "private" / "current"
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

    def test_private_scheduler_invokes_stable_scheduled_entrypoint(self) -> None:
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
                "run-scheduled",
                "--mode",
                "private",
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
                github_release_asset(101, f"personal-codex-{SHA1}.tar.gz"),
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.sha, SHA1)
        self.assertEqual(assets.archive_name, f"personal-codex-{SHA1}.tar.gz")
        self.assertEqual((assets.archive_id, assets.archive_size), (101, 1))
        self.assertEqual(assets.checksum_name, f"personal-codex-{SHA1}.sha256")
        self.assertEqual((assets.checksum_id, assets.checksum_size), (102, 1))

    def test_select_release_assets_rejects_unique_pending_pair(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA1,
            "assets": [
                github_release_asset(
                    101,
                    f"personal-codex-{SHA1}.tar.gz",
                    state="new",
                ),
                github_release_asset(
                    102,
                    f"personal-codex-{SHA1}.sha256",
                    state="new",
                ),
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "not uploaded"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_missing_asset_state(self) -> None:
        archive = github_release_asset(101, f"personal-codex-{SHA1}.tar.gz")
        archive.pop("state")
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA1,
            "assets": [
                archive,
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "not uploaded"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_extra_pending_matching_assets(
        self,
    ) -> None:
        cases = (
            ("duplicate-archive", f"personal-codex-{SHA1}.tar.gz"),
            ("other-archive", f"personal-codex-{SHA2}.tar.gz"),
            ("duplicate-checksum", f"personal-codex-{SHA1}.sha256"),
            ("other-checksum", f"personal-codex-{SHA2}.sha256"),
        )
        for name, pending_name in cases:
            with self.subTest(name=name):
                release = {
                    "tagName": "personal-codex-20260511-120000-1111111",
                    "targetCommitish": SHA1,
                    "assets": [
                        github_release_asset(
                            101,
                            f"personal-codex-{SHA1}.tar.gz",
                        ),
                        github_release_asset(
                            102,
                            f"personal-codex-{SHA1}.sha256",
                        ),
                        github_release_asset(999, pending_name, state="new"),
                    ],
                }

                with self.assertRaisesRegex(MODULE.SyncError, "not uploaded"):
                    MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_extra_uploaded_other_sha_assets(
        self,
    ) -> None:
        cases = (
            ("other-archive", f"personal-codex-{SHA2}.tar.gz"),
            ("other-checksum", f"personal-codex-{SHA2}.sha256"),
        )
        for name, extra_name in cases:
            with self.subTest(name=name):
                release = {
                    "tagName": "personal-codex-20260511-120000-1111111",
                    "targetCommitish": SHA1,
                    "assets": [
                        github_release_asset(
                            101,
                            f"personal-codex-{SHA1}.tar.gz",
                        ),
                        github_release_asset(
                            102,
                            f"personal-codex-{SHA1}.sha256",
                        ),
                        github_release_asset(999, extra_name),
                    ],
                }

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "multiple tarball assets|exactly one personal-codex",
                ):
                    MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_invalid_api_metadata(self) -> None:
        archive_name = f"personal-codex-{SHA1}.tar.gz"
        checksum_name = f"personal-codex-{SHA1}.sha256"
        cases = (
            (
                "missing-id",
                {"name": archive_name, "size": 1, "state": "uploaded"},
                "asset id",
            ),
            (
                "boolean-id",
                github_release_asset(True, archive_name),
                "asset id",
            ),
            (
                "zero-id",
                github_release_asset(0, archive_name),
                "asset id",
            ),
            (
                "missing-size",
                {"id": 101, "name": archive_name, "state": "uploaded"},
                "asset size",
            ),
            (
                "boolean-size",
                github_release_asset(101, archive_name, size=False),
                "asset size",
            ),
            (
                "negative-size",
                github_release_asset(101, archive_name, size=-1),
                "asset size",
            ),
            (
                "oversized",
                github_release_asset(
                    101,
                    archive_name,
                    size=MODULE.MAX_ARCHIVE_COMPRESSED_BYTES + 1,
                ),
                "exceeds",
            ),
        )
        checksum = github_release_asset(102, checksum_name)

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
            "assets": [github_release_asset(101, f"personal-codex-{SHA1}.tar.gz")],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "missing checksum"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_multiple_tarballs(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "assets": [
                github_release_asset(101, f"personal-codex-{SHA1}.tar.gz"),
                github_release_asset(201, f"personal-codex-{SHA2}.tar.gz"),
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
                github_release_asset(202, f"personal-codex-{SHA2}.sha256"),
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
                github_release_asset(101, f"personal-codex-{SHA1}.tar.gz"),
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.archive_name, f"personal-codex-{SHA1}.tar.gz")
        self.assertEqual(assets.checksum_name, f"personal-codex-{SHA1}.sha256")

    def test_select_release_assets_rejects_tag_sha_mismatch(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-2222222",
            "assets": [
                github_release_asset(101, f"personal-codex-{SHA1}.tar.gz"),
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "does not match tag suffix"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_rejects_target_commit_mismatch(self) -> None:
        release = {
            "tagName": "personal-codex-20260511-120000-1111111",
            "targetCommitish": SHA2,
            "assets": [
                github_release_asset(101, f"personal-codex-{SHA1}.tar.gz"),
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
            ],
        }

        with self.assertRaisesRegex(MODULE.SyncError, "does not match target commit"):
            MODULE.select_release_assets(release)

    def test_select_release_assets_accepts_github_api_payload(self) -> None:
        release = {
            "tag_name": "personal-codex-20260511-120000-1111111",
            "target_commitish": SHA1,
            "assets": [
                github_release_asset(101, f"personal-codex-{SHA1}.tar.gz"),
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
            ],
        }

        assets = MODULE.select_release_assets(release)

        self.assertEqual(assets.sha, SHA1)

    def test_run_gh_json_wraps_missing_gh(self) -> None:
        with mock.patch.object(
            MODULE.subprocess,
            "Popen",
            side_effect=FileNotFoundError("No such file or directory"),
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError, "GitHub CLI `gh` is not available"
            ):
                MODULE._run_gh_json(["api", "repos/owner/repo/releases"])

    def test_run_gh_wraps_missing_gh(self) -> None:
        with mock.patch.object(
            MODULE.subprocess,
            "Popen",
            side_effect=FileNotFoundError("No such file or directory"),
        ):
            with self.assertRaisesRegex(
                MODULE.SyncError, "GitHub CLI `gh` is not available"
            ):
                MODULE._run_gh(["release", "download", "tag"])

    def test_process_guardian_reports_target_and_remains_live_for_fence(
        self,
    ) -> None:
        process = MODULE._spawn_guarded_process(
            [
                sys.executable,
                "-c",
                (
                    "import os;os.write(1,b'guardian-stdout');"
                    "os.write(2,b'guardian-stderr')"
                ),
            ],
            deadline=time.monotonic() + 5.0,
            process_label="test process",
            unavailable_code="test-unavailable",
            unavailable_message="test executable unavailable",
        )
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        status_payload = process.status.read(MODULE.PROCESS_GUARDIAN_STATUS_RECORD.size)

        self.assertEqual(stdout, b"guardian-stdout")
        self.assertEqual(stderr, b"guardian-stderr")
        self.assertEqual(
            len(status_payload), MODULE.PROCESS_GUARDIAN_STATUS_RECORD.size
        )
        magic, guardian_pid, target_pid, target_returncode = (
            MODULE.PROCESS_GUARDIAN_STATUS_RECORD.unpack(status_payload)
        )
        self.assertEqual(magic, MODULE.PROCESS_GUARDIAN_STATUS_MAGIC)
        self.assertEqual(guardian_pid, process.pid)
        self.assertEqual(target_pid, process.target_pid)
        self.assertEqual(target_returncode, 0)
        self.assertIsNone(process.returncode)
        os.set_blocking(process.status.fileno(), False)
        with self.assertRaises(BlockingIOError):
            os.read(process.status.fileno(), 1)

        receipt = MODULE._terminalize_process_group_before_reap(
            process,
            deadline=time.monotonic() + 5.0,
            process_label="test process guardian",
        )
        close_failures = MODULE._close_process_supervision_resources(
            process,
            None,
            process_label="test process guardian",
        )

        self.assertTrue(receipt.complete, receipt.errors)
        self.assertTrue(receipt.kill_sent)
        self.assertEqual(receipt.returncode, -9)
        self.assertEqual(close_failures, [])

    def test_guardian_terminalization_never_touches_group_after_wait(self) -> None:
        lifecycle: list[str] = []
        guardian = mock.Mock()
        guardian.returncode = None
        process = mock.Mock(pid=12345, returncode=None, guardian=guardian)

        def kill_group(process_group_id, signal_number):
            self.assertEqual(process_group_id, 12345)
            self.assertEqual(signal_number, MODULE.signal.SIGKILL)
            self.assertEqual(lifecycle, [])
            lifecycle.append("killpg")

        def wait(*, timeout):
            self.assertGreaterEqual(timeout, 0.0)
            self.assertEqual(lifecycle, ["killpg"])
            lifecycle.append("wait")
            return -MODULE.signal.SIGKILL

        process.wait.side_effect = wait
        with mock.patch.object(MODULE.os, "killpg", side_effect=kill_group):
            receipt = MODULE._terminalize_process_group_before_reap(
                process,
                deadline=time.monotonic() + 1.0,
                process_label="test guardian",
            )

        self.assertTrue(receipt.complete, receipt.errors)
        self.assertEqual(lifecycle, ["killpg", "wait"])
        process.wait.assert_called_once()

    def test_guardian_terminalization_rejects_early_exit_and_signal_errors(
        self,
    ) -> None:
        early = mock.Mock(pid=12345, returncode=0)
        with mock.patch.object(MODULE.os, "killpg") as kill_group:
            receipt = MODULE._terminalize_process_group_before_reap(
                early,
                deadline=time.monotonic() + 1.0,
                process_label="early guardian",
            )
        self.assertFalse(receipt.complete)
        kill_group.assert_not_called()
        early.wait.assert_not_called()

        for signal_error in (
            ProcessLookupError("missing group"),
            PermissionError("denied group"),
            OSError("other group failure"),
        ):
            with self.subTest(error=type(signal_error).__name__):
                process = mock.Mock(pid=12345, returncode=None)
                process.wait.return_value = -MODULE.signal.SIGKILL
                with mock.patch.object(
                    MODULE.os,
                    "killpg",
                    side_effect=signal_error,
                ):
                    receipt = MODULE._terminalize_process_group_before_reap(
                        process,
                        deadline=time.monotonic() + 1.0,
                        process_label="failed guardian",
                    )
                self.assertFalse(receipt.complete)
                process.wait.assert_called_once()

    def test_guardian_status_is_exact_and_identity_bound(self) -> None:
        process = mock.Mock(pid=12345, target_pid=23456)
        valid = MODULE.PROCESS_GUARDIAN_STATUS_RECORD.pack(
            MODULE.PROCESS_GUARDIAN_STATUS_MAGIC,
            process.pid,
            process.target_pid,
            0,
        )
        self.assertEqual(
            MODULE._parse_guardian_status(
                process,
                valid,
                process_label="test process",
                error_code="test-protocol",
            ),
            0,
        )
        invalid_payloads = (
            valid[:-1],
            valid + b"x",
            MODULE.PROCESS_GUARDIAN_STATUS_RECORD.pack(
                b"BADMAGIC",
                process.pid,
                process.target_pid,
                0,
            ),
            MODULE.PROCESS_GUARDIAN_STATUS_RECORD.pack(
                MODULE.PROCESS_GUARDIAN_STATUS_MAGIC,
                process.pid + 1,
                process.target_pid,
                0,
            ),
            MODULE.PROCESS_GUARDIAN_STATUS_RECORD.pack(
                MODULE.PROCESS_GUARDIAN_STATUS_MAGIC,
                process.pid,
                process.target_pid + 1,
                0,
            ),
            MODULE.PROCESS_GUARDIAN_STATUS_RECORD.pack(
                MODULE.PROCESS_GUARDIAN_STATUS_MAGIC,
                process.pid,
                process.target_pid,
                256,
            ),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(MODULE.SyncError) as raised:
                    MODULE._parse_guardian_status(
                        process,
                        payload,
                        process_label="test process",
                        error_code="test-protocol",
                    )
                self.assertEqual(raised.exception.code, "test-protocol")

    def test_guardian_status_writer_eof_is_early_exit(self) -> None:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        status = os.fdopen(read_fd, "rb", buffering=0)
        process = mock.Mock(status=status)
        os.set_blocking(status.fileno(), False)
        try:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "exited before process-group fencing",
            ) as raised:
                MODULE._require_guardian_status_writer_live(
                    process,
                    process_label="test process",
                    error_code="test-protocol",
                )
            self.assertEqual(raised.exception.code, "test-protocol")
        finally:
            status.close()

    def test_guardian_target_launch_failure_preserves_unavailable_taxonomy(
        self,
    ) -> None:
        guardians: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def capture_guardian(args, **kwargs):
            guardian = real_popen(args, **kwargs)
            guardians.append(guardian)
            return guardian

        with (
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_guardian,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "test executable unavailable",
            ) as raised,
        ):
            MODULE._spawn_guarded_process(
                ["/definitely/missing/codex-personal-sync-target"],
                deadline=time.monotonic() + 5.0,
                process_label="missing target",
                unavailable_code="test-unavailable",
                unavailable_message="test executable unavailable",
            )

        self.assertEqual(raised.exception.code, "test-unavailable")
        self.assertEqual(len(guardians), 1)
        self.assertEqual(guardians[0].returncode, -MODULE.signal.SIGKILL)
        self.assertTrue(guardians[0].stdout.closed)
        self.assertTrue(guardians[0].stderr.closed)

    def test_guardian_control_descriptors_do_not_reach_target(self) -> None:
        process = MODULE._spawn_guarded_process(
            [
                sys.executable,
                "-c",
                (
                    "import os;leaks=[];"
                    'exec("for fd in range(3, 64):\\n'
                    " try:\\n  os.fstat(fd)\\n  leaks.append(fd)\\n"
                    ' except OSError:\\n  pass");'
                    "os.write(1,(','.join(map(str,leaks))).encode())"
                ),
            ],
            deadline=time.monotonic() + 5.0,
            process_label="descriptor test",
            unavailable_code="test-unavailable",
            unavailable_message="test executable unavailable",
        )
        self.assertEqual(process.stdout.read(), b"")
        self.assertEqual(process.stderr.read(), b"")
        status = process.status.read(MODULE.PROCESS_GUARDIAN_STATUS_RECORD.size)
        self.assertEqual(len(status), MODULE.PROCESS_GUARDIAN_STATUS_RECORD.size)
        receipt = MODULE._terminalize_process_group_before_reap(
            process,
            deadline=time.monotonic() + 5.0,
            process_label="descriptor test guardian",
        )
        MODULE._close_process_supervision_resources(
            process,
            None,
            process_label="descriptor test guardian",
        )
        self.assertTrue(receipt.complete, receipt.errors)

    def test_truncated_guardian_ready_receipt_is_fenced_before_reap(self) -> None:
        guardians: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen
        truncated_guardian = """
import os
import signal
import sys
ready_fd = int(sys.argv[1])
status_fd = int(sys.argv[2])
os.write(ready_fd, b'x')
os.close(ready_fd)
os.close(status_fd)
os.close(1)
os.close(2)
while True:
    signal.pause()
"""

        def capture_guardian(args, **kwargs):
            guardian = real_popen(args, **kwargs)
            guardians.append(guardian)
            return guardian

        with (
            mock.patch.object(
                MODULE,
                "PROCESS_GUARDIAN_SOURCE",
                truncated_guardian,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_guardian,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "ready receipt is incomplete",
            ) as raised,
        ):
            MODULE._spawn_guarded_process(
                [sys.executable, "-c", "pass"],
                deadline=time.monotonic() + 5.0,
                process_label="truncated guardian",
                unavailable_code="test-unavailable",
                unavailable_message="test executable unavailable",
            )

        self.assertEqual(raised.exception.code, "process-guardian-protocol")
        self.assertEqual(len(guardians), 1)
        self.assertEqual(guardians[0].returncode, -MODULE.signal.SIGKILL)

    def test_second_guardian_pipe_failure_closes_first_pipe_pair(self) -> None:
        real_pipe = os.pipe
        first_pair: tuple[int, int] | None = None
        calls = 0

        def fail_second_pipe():
            nonlocal calls, first_pair
            calls += 1
            if calls == 1:
                first_pair = real_pipe()
                return first_pair
            raise OSError("injected second pipe failure")

        with (
            mock.patch.object(MODULE.os, "pipe", side_effect=fail_second_pipe),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "guardian control pipes",
            ) as raised,
        ):
            MODULE._spawn_guarded_process(
                [sys.executable, "-c", "pass"],
                deadline=time.monotonic() + 5.0,
                process_label="pipe failure",
                unavailable_code="test-unavailable",
                unavailable_message="test executable unavailable",
            )

        self.assertEqual(raised.exception.code, "process-guardian-protocol")
        self.assertIsNotNone(first_pair)
        assert first_pair is not None
        for file_descriptor in first_pair:
            with self.assertRaises(OSError):
                os.fstat(file_descriptor)

    def test_ready_reader_owns_fd_across_exception_and_number_reuse(self) -> None:
        real_reader = MODULE._read_guardian_ready_record
        reused_fd: int | None = None

        def read_then_reuse(file_descriptor, **kwargs):
            nonlocal reused_fd
            real_reader(file_descriptor, **kwargs)
            reused_fd = os.open("/dev/null", os.O_RDONLY)
            self.assertEqual(reused_fd, file_descriptor)
            raise MODULE.SyncError(
                "injected post-ready failure",
                code="process-guardian-protocol",
            )

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_read_guardian_ready_record",
                    side_effect=read_then_reuse,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "injected post-ready failure",
                ),
            ):
                MODULE._spawn_guarded_process(
                    [sys.executable, "-c", "pass"],
                    deadline=time.monotonic() + 5.0,
                    process_label="ready ownership",
                    unavailable_code="test-unavailable",
                    unavailable_message="test executable unavailable",
                )
            self.assertIsNotNone(reused_fd)
            assert reused_fd is not None
            os.fstat(reused_fd)
        finally:
            if reused_fd is not None:
                os.close(reused_fd)

    def test_ready_reader_close_failure_preserves_protocol_primary(self) -> None:
        read_fd, write_fd = os.pipe()
        real_selector = MODULE.selectors.DefaultSelector
        try:
            with (
                mock.patch.object(
                    MODULE.selectors,
                    "DefaultSelector",
                    return_value=CloseFailingSelector(
                        real_selector(),
                        RuntimeError("injected ready selector close failure"),
                    ),
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "ready receipt timed out.*ready selector close failure",
                ) as raised,
            ):
                MODULE._read_guardian_ready_record(
                    read_fd,
                    deadline=time.monotonic() - 1.0,
                    process_label="ready close test",
                )
            read_fd = -1
            self.assertEqual(raised.exception.code, "process-guardian-protocol")
            self.assertIsInstance(raised.exception.__cause__, MODULE.SyncError)
            self.assertIn(
                "ready receipt timed out",
                str(raised.exception.__cause__),
            )
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            os.close(write_fd)

    def test_bounded_gh_selector_close_failure_preserves_primary(self) -> None:
        process = FakeDownloadProcess(b"123456789")
        real_selector = MODULE.selectors.DefaultSelector
        selector_calls = 0

        def selector_factory():
            nonlocal selector_calls
            selector_calls += 1
            if selector_calls == 1:
                return CloseFailingSelector(
                    real_selector(),
                    RuntimeError("injected selector close failure"),
                )
            return real_selector()

        with (
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                return_value=process,
            ),
            mock.patch.object(
                MODULE.selectors,
                "DefaultSelector",
                side_effect=selector_factory,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "stdout exceeds.*selector close failure",
            ) as raised,
        ):
            MODULE._run_bounded_gh_process(
                ["api", "repos/owner/repo/releases"],
                deadline=time.monotonic() + 5.0,
                stdout_limit=8,
                stderr_limit=8,
                label="gh metadata command",
            )

        self.assertEqual(raised.exception.code, "gh-cleanup-inconclusive")
        self.assertIsInstance(raised.exception.__cause__, MODULE.SyncError)
        self.assertEqual(raised.exception.__cause__.code, "gh-stdout-limit")
        self.assertEqual(process.returncode, -MODULE.signal.SIGKILL)

    def test_gh_maps_guardian_spawn_cleanup_failure_to_lane_taxonomy(self) -> None:
        primary = MODULE.SyncError(
            "injected guardian cleanup failure",
            code="process-guardian-cleanup-inconclusive",
        )
        with (
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                side_effect=primary,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "guardian cleanup was inconclusive",
            ) as raised,
        ):
            MODULE._run_bounded_gh_process(
                ["api", "repos/owner/repo/releases"],
                deadline=time.monotonic() + 5.0,
                stdout_limit=8,
                stderr_limit=8,
                label="gh metadata command",
            )

        self.assertEqual(raised.exception.code, "gh-cleanup-inconclusive")
        self.assertIs(raised.exception.__cause__, primary)

    def test_gh_maps_guardian_operation_deadline_to_lane_taxonomy(self) -> None:
        primary = MODULE.SyncError(
            "gh metadata command exceeded its monotonic deadline",
            code="process-guardian-operation-timeout",
        )
        with (
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                side_effect=primary,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "gh metadata command exceeded its monotonic deadline",
            ) as raised,
        ):
            MODULE._run_bounded_gh_process(
                ["api", "repos/owner/repo/releases"],
                deadline=time.monotonic() + 1.0,
                stdout_limit=8,
                stderr_limit=8,
                label="gh metadata command",
            )

        self.assertEqual(raised.exception.code, "gh-timeout")
        self.assertIs(raised.exception.__cause__, primary)

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

    def test_run_gh_metadata_enforces_output_cap_and_reaps_group(self) -> None:
        real_spawn = MODULE._spawn_guarded_process
        processes: list[MODULE._GuardedProcess] = []

        def overflowing_spawn(_args, **kwargs):
            process = real_spawn(
                [
                    sys.executable,
                    "-c",
                    "import os,time;os.write(1,b'123456789');time.sleep(30)",
                ],
                deadline=kwargs["deadline"],
                process_label=kwargs["process_label"],
                unavailable_code="test-unavailable",
                unavailable_message="test executable unavailable",
            )
            processes.append(process)
            return process

        with (
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                side_effect=overflowing_spawn,
            ),
            mock.patch.object(MODULE, "MAX_GH_METADATA_STDOUT_BYTES", 8),
            mock.patch.object(MODULE, "GH_OPERATION_TIMEOUT_SECONDS", 1.0),
            mock.patch.object(MODULE, "GH_CLEANUP_TIMEOUT_SECONDS", 1.0),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "stdout exceeds the 8-byte limit",
            ) as raised,
        ):
            MODULE._run_gh_process(["api", "repos/owner/repo/releases"])

        self.assertEqual(raised.exception.code, "gh-stdout-limit")
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, -9)

    def test_download_release_asset_times_out_and_reaps_stalled_group(self) -> None:
        real_spawn = MODULE._spawn_guarded_process
        processes: list[MODULE._GuardedProcess] = []

        def stalled_spawn(_args, **kwargs):
            process = real_spawn(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                deadline=kwargs["deadline"],
                process_label=kwargs["process_label"],
                unavailable_code="test-unavailable",
                unavailable_message="test executable unavailable",
            )
            processes.append(process)
            return process

        payload = b"x"
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=len(payload),
            checksum_id=102,
            checksum_size=len(payload),
            archive_digest=github_sha256(payload),
            checksum_digest=github_sha256(payload),
        )
        destination = self.root / "stalled-download"
        with (
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                side_effect=stalled_spawn,
            ),
            mock.patch.object(MODULE, "GH_OPERATION_TIMEOUT_SECONDS", 1.0),
            mock.patch.object(MODULE, "GH_CLEANUP_TIMEOUT_SECONDS", 1.0),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exceeded its monotonic deadline",
            ) as raised,
        ):
            self.download_release_assets("owner/repo", assets, destination)

        self.assertEqual(raised.exception.code, "gh-timeout")
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, -9)
        self.assertFalse((destination / assets.archive_name).exists())
        self.assertEqual(list(destination.glob(".*.partial.*")), [])

    def test_gh_cleanup_inconclusive_preserves_primary_classification(self) -> None:
        process = FakeDownloadProcess(b"123456789")
        incomplete = MODULE._GhCleanupReceipt(
            kill_sent=True,
            child_reaped=False,
            stdout_drained=False,
            stderr_drained=True,
            status_drained=False,
            process_group_fenced=False,
            errors=("injected cleanup failure",),
        )
        with (
            mock.patch.object(MODULE, "_spawn_guarded_process", return_value=process),
            mock.patch.object(
                MODULE,
                "_cleanup_gh_process_group",
                return_value=incomplete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "cleanup was inconclusive.*stdout-not-drained",
            ) as raised,
        ):
            MODULE._run_bounded_gh_process(
                ["api", "repos/owner/repo/releases"],
                deadline=time.monotonic() + 1,
                stdout_limit=8,
                stderr_limit=8,
                label="gh metadata command",
            )

        self.assertEqual(raised.exception.code, "gh-cleanup-inconclusive")
        self.assertIn("stdout exceeds", str(raised.exception.__cause__))
        terminalization = MODULE._terminalize_process_group_before_reap(
            process,
            deadline=time.monotonic() + 5.0,
            process_label="test cleanup guardian",
        )
        self.assertTrue(terminalization.complete, terminalization.errors)

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
            archive_digest=github_sha256(archive_payload),
            checksum_digest=github_sha256(checksum_payload),
        )

        def fake_popen(args, **_kwargs):
            calls.append(args)
            return processes.pop(0)

        destination = self.root / "downloads"
        with mock.patch.object(
            MODULE, "_spawn_guarded_process", side_effect=fake_popen
        ):
            self.download_release_assets("owner/repo", assets, destination)

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
        self.assertEqual(
            (destination / assets.archive_name).read_bytes(), archive_payload
        )
        self.assertEqual(
            (destination / assets.checksum_name).read_bytes(), checksum_payload
        )
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
            archive_digest=github_sha256(payload),
            checksum_digest="sha256:" + "0" * 64,
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
            mock.patch.object(MODULE, "_spawn_guarded_process", return_value=process),
            mock.patch.object(
                MODULE.os, "link", side_effect=replace_partial_before_link
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed during publication",
            ),
        ):
            self.download_release_assets("owner/repo", assets, destination)

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
        self.assertEqual(
            {entry.read_bytes() for entry in retained}, {b"forged-payload"}
        )

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
            archive_digest=github_sha256(payload),
            checksum_digest="sha256:" + "0" * 64,
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
            mock.patch.object(MODULE, "_spawn_guarded_process", return_value=process),
            mock.patch.object(MODULE.os, "link", side_effect=replace_target_after_link),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed during publication",
            ),
        ):
            self.download_release_assets("owner/repo", assets, destination)

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
        self.assertEqual(
            {entry.read_bytes() for entry in retained}, {b"forged-payload"}
        )

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
            archive_digest=github_sha256(payload),
            checksum_digest="sha256:" + "0" * 64,
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
            mock.patch.object(MODULE, "_spawn_guarded_process", return_value=process),
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
            self.download_release_assets("owner/repo", assets, destination)

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
        self.assertEqual(
            {entry.read_bytes() for entry in retained}, {b"forged-payload"}
        )

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
            archive_digest="sha256:" + "0" * 64,
            checksum_digest="sha256:" + "0" * 64,
        )
        destination = self.root / "cleanup-error-download"
        real_fsync = os.fsync

        def fail_directory_fsync(file_descriptor):
            if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                raise OSError("injected directory fsync failure")
            return real_fsync(file_descriptor)

        with (
            mock.patch.object(MODULE, "_spawn_guarded_process", return_value=process),
            mock.patch.object(MODULE.os, "fsync", side_effect=fail_directory_fsync),
            mock.patch.object(
                MODULE,
                "_close_fd_quietly",
                wraps=MODULE._close_fd_quietly,
            ) as close_fd,
            self.assertRaisesRegex(MODULE.SyncError, "size mismatch"),
        ):
            self.download_release_assets("owner/repo", assets, destination)

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
            archive_digest="sha256:" + "0" * 64,
            checksum_digest="sha256:" + "0" * 64,
        )
        destination = self.root / "oversized-download"

        with mock.patch.object(MODULE, "_spawn_guarded_process", return_value=process):
            with self.assertRaisesRegex(MODULE.SyncError, "exceeds its advertised"):
                self.download_release_assets("owner/repo", assets, destination)

        self.assertEqual(process.returncode, -9)
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
            archive_digest="sha256:" + "0" * 64,
            checksum_digest="sha256:" + "0" * 64,
        )
        destination = self.root / "short-download"

        with mock.patch.object(MODULE, "_spawn_guarded_process", return_value=process):
            with self.assertRaisesRegex(MODULE.SyncError, "size mismatch"):
                self.download_release_assets("owner/repo", assets, destination)

        self.assertFalse((destination / assets.archive_name).exists())
        self.assertEqual(list(destination.glob(".*.partial.*")), [])

    def test_download_release_assets_validates_all_metadata_before_starting(
        self,
    ) -> None:
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=1,
            checksum_id=102,
            checksum_size=MODULE.MAX_ARCHIVE_CHECKSUM_BYTES + 1,
            archive_digest="sha256:" + "0" * 64,
            checksum_digest="sha256:" + "0" * 64,
        )
        destination = self.root / "invalid-metadata-download"

        with mock.patch.object(MODULE, "_spawn_guarded_process") as popen:
            with self.assertRaisesRegex(MODULE.SyncError, "exceeds"):
                self.download_release_assets("owner/repo", assets, destination)

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
        archive_metadata = archive.stat()
        archive_identity = (
            archive_metadata.st_dev,
            archive_metadata.st_ino,
            archive_metadata.st_size,
        )
        real_read = os.read
        rewritten = False

        def rewrite_after_archive_read(file_descriptor, size):
            nonlocal rewritten
            metadata = os.fstat(file_descriptor)
            is_archive = (metadata.st_dev, metadata.st_ino) == archive_identity[:2]
            payload = real_read(file_descriptor, min(size, 1) if is_archive else size)
            if is_archive and payload and not rewritten:
                rewritten = True
                writer_fd = os.open(archive, os.O_RDWR)
                try:
                    # Rewrite a byte that the bounded first read has not copied yet.
                    os.lseek(writer_fd, 1, os.SEEK_SET)
                    os.write(writer_fd, b"X")
                    os.fsync(writer_fd)
                finally:
                    os.close(writer_fd)
            return payload

        with mock.patch.object(MODULE.os, "read", rewrite_after_archive_read):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "compressed archive changed while reading|checksum mismatch",
            ):
                MODULE.verify_checksum(archive, checksum)

        self.assertTrue(rewritten)
        final_metadata = archive.stat()
        self.assertEqual(
            (final_metadata.st_dev, final_metadata.st_ino, final_metadata.st_size),
            archive_identity,
        )

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

        def replace_archive_path(snapshot, extract_root, *, workspace):
            archive_path.rename(retained_archive)
            malicious_archive.rename(archive_path)
            return real_extract(
                snapshot,
                extract_root,
                workspace=workspace,
            )

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
            release = self.download_and_extract_release("owner/repo", destination)

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

    def test_expected_release_file_rejects_release_root_name_replacement(
        self,
    ) -> None:
        release_root = self.root / "expected-release"
        retained_release = self.root / "retained-expected-release"
        runner_path = MODULE.PurePosixPath("scripts/codex_personal_sync.py")
        write_minimal_release(release_root)
        original_runner = (release_root / Path(*runner_path.parts)).read_bytes()
        release_expectation = MODULE._source_release_identity(release_root, None)

        self.assertEqual(
            MODULE.read_expected_release_file(
                release_root,
                runner_path,
                release_expectation,
            ),
            original_runner,
        )

        release_root.rename(retained_release)
        write_minimal_release(release_root, agent_text="substitute\n")
        (release_root / Path(*runner_path.parts)).write_text(
            "#!/usr/bin/env python3\n# substitute\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "release source changed after its captured identity",
        ):
            MODULE.read_expected_release_file(
                release_root,
                runner_path,
                release_expectation,
            )

    def test_expected_release_file_rejects_descendant_replace_restore(
        self,
    ) -> None:
        release_root = self.root / "expected-descendant-release"
        substitute_root = self.root / "substitute-descendant-release"
        retained_scripts = self.root / "retained-expected-scripts"
        runner_path = MODULE.PurePosixPath("scripts/codex_personal_sync.py")
        write_minimal_release(release_root)
        write_minimal_release(substitute_root)
        (substitute_root / Path(*runner_path.parts)).write_text(
            "#!/usr/bin/env python3\n# install-private substitute\n",
            encoding="utf-8",
        )
        release_expectation = MODULE._source_release_identity(release_root, None)
        original_runner = (release_root / Path(*runner_path.parts)).read_bytes()
        real_capture = (
            MODULE._release_tree_identity_and_captured_files_from_directory_fd
        )

        def capture_substitute_then_restore(*args, **kwargs):
            original_scripts = release_root / "scripts"
            substitute_scripts = substitute_root / "scripts"
            original_scripts.rename(retained_scripts)
            substitute_scripts.rename(original_scripts)
            try:
                return real_capture(*args, **kwargs)
            finally:
                original_scripts.rename(substitute_scripts)
                retained_scripts.rename(original_scripts)

        with (
            mock.patch.object(
                MODULE,
                "_release_tree_identity_and_captured_files_from_directory_fd",
                side_effect=capture_substitute_then_restore,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "release source changed after its captured identity",
            ),
        ):
            MODULE.read_expected_release_file(
                release_root,
                runner_path,
                release_expectation,
            )

        self.assertEqual(
            (release_root / Path(*runner_path.parts)).read_bytes(),
            original_runner,
        )

    def test_download_extract_rejects_destination_replacement_after_download(
        self,
    ) -> None:
        destination = self.root / "replaceable-download"
        retained_destination = self.root / "retained-download"
        source_root = self.root / "download-replacement-source"
        source_archive = self.root / "download-replacement.tar.gz"
        source_checksum = self.root / "download-replacement.sha256"
        write_minimal_release(source_root)
        with tarfile.open(source_archive, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        digest = hashlib.sha256(source_archive.read_bytes()).hexdigest()
        source_checksum.write_text(
            f"{digest}  personal-codex-{SHA1}.tar.gz\n",
            encoding="utf-8",
        )
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260520-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=1,
            archive_size=source_archive.stat().st_size,
            checksum_id=2,
            checksum_size=source_checksum.stat().st_size,
        )

        def download_then_replace(repo, selected_assets, path, *, workspace):
            self.assertEqual(repo, "owner/repo")
            self.assertEqual(selected_assets, assets)
            self.assertEqual(workspace.path, path)
            shutil.copy2(source_archive, path / assets.archive_name)
            shutil.copy2(source_checksum, path / assets.checksum_name)
            path.rename(retained_destination)
            path.mkdir()
            (path / "replacement.txt").write_text(
                "replacement\n",
                encoding="utf-8",
            )

        with (
            mock.patch.object(MODULE, "find_latest_release", return_value={}),
            mock.patch.object(MODULE, "select_release_assets", return_value=assets),
            mock.patch.object(
                MODULE,
                "download_release_assets",
                side_effect=download_then_replace,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "archive workspace binding changed",
            ),
        ):
            self.download_and_extract_release("owner/repo", destination)

        self.assertFalse((retained_destination / "extract").exists())
        self.assertEqual(
            (destination / "replacement.txt").read_text(encoding="utf-8"),
            "replacement\n",
        )

    def test_verify_extract_rejects_symlinked_workspace_descendant_without_writes(
        self,
    ) -> None:
        trusted_root = self.root / "trusted-workspace"
        outside = self.root / "outside"
        source_root = self.root / "symlink-source"
        trusted_root.mkdir()
        outside.mkdir()
        write_minimal_release(source_root)
        link = trusted_root / "link"
        link.symlink_to(outside, target_is_directory=True)
        archive_path = link / f"personal-codex-{SHA1}.tar.gz"
        checksum_path = link / f"personal-codex-{SHA1}.sha256"
        with tarfile.open(outside / archive_path.name, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        digest = hashlib.sha256((outside / archive_path.name).read_bytes()).hexdigest()
        (outside / checksum_path.name).write_text(
            f"{digest}  {archive_path.name}\n",
            encoding="utf-8",
        )

        def outside_snapshot() -> dict[str, tuple[int, bytes | None]]:
            snapshot: dict[str, tuple[int, bytes | None]] = {}
            for path in sorted(outside.rglob("*")):
                metadata = path.lstat()
                snapshot[path.relative_to(outside).as_posix()] = (
                    stat.S_IFMT(metadata.st_mode),
                    path.read_bytes() if path.is_file() else None,
                )
            return snapshot

        before = outside_snapshot()
        with MODULE.bind_archive_workspace(trusted_root) as workspace:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "unsafe (?:checksum file parent|archive directory)",
            ):
                MODULE.verify_and_extract_archive(
                    archive_path,
                    checksum_path,
                    link / "extract",
                    workspace=workspace,
                )

        self.assertEqual(outside_snapshot(), before)

    def test_temporary_archive_workspace_binds_and_cleans_random_leaf(self) -> None:
        parent = self.root / "temporary-workspace-parent"
        parent.mkdir()
        workspace_path: Path | None = None
        workspace_fd = -1

        with MODULE.temporary_archive_workspace(
            prefix="temporary-workspace.",
            parent=parent,
        ) as workspace:
            workspace_path = workspace.path
            workspace_fd = workspace.fd
            metadata = workspace_path.stat()
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                workspace.identity,
            )
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
            (workspace_path / "nested").mkdir()
            (workspace_path / "nested" / "payload.txt").write_text(
                "payload\n",
                encoding="utf-8",
            )

        assert workspace_path is not None
        self.assertFalse(workspace_path.exists())
        with self.assertRaises(OSError):
            os.fstat(workspace_fd)

    def test_temporary_archive_workspace_accepts_symlinked_parent_ancestor(
        self,
    ) -> None:
        real_root = self.root / "real-temporary-root"
        real_parent = real_root / "tmp"
        alias_root = self.root / "alias-temporary-root"
        real_parent.mkdir(parents=True)
        alias_root.symlink_to(real_root, target_is_directory=True)
        lexical_parent = alias_root / "tmp"

        with MODULE.temporary_archive_workspace(
            prefix="ancestor-symlink.",
            parent=lexical_parent,
        ) as workspace:
            self.assertEqual(
                workspace.path.parent,
                Path(os.path.abspath(lexical_parent)),
            )
            self.assertNotEqual(workspace.path.parent, real_parent)
            self.assertTrue(workspace.path.is_dir())

    def test_temporary_archive_workspace_normalizes_macos_system_temp_alias(
        self,
    ) -> None:
        canonical_parent = self.root / "private-tmp"
        alias_parent = self.root / "tmp"
        canonical_parent.mkdir(mode=0o700)
        canonical_parent = canonical_parent.resolve()
        alias_parent.symlink_to(canonical_parent, target_is_directory=True)

        workspace_path: Path | None = None
        with (
            mock.patch.object(
                MODULE,
                "_uses_macos_system_temp_alias",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_ALIAS",
                alias_parent,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_DIRECTORY",
                canonical_parent,
            ),
            mock.patch.object(
                MODULE.tempfile,
                "gettempdir",
                return_value=str(alias_parent),
            ),
        ):
            with MODULE.temporary_archive_workspace(
                prefix="macos-system-temp."
            ) as workspace:
                workspace_path = workspace.path
                self.assertEqual(workspace.path.parent, canonical_parent)
                self.assertEqual(stat.S_IMODE(workspace.path.stat().st_mode), 0o700)

        assert workspace_path is not None
        self.assertFalse(workspace_path.exists())
        self.assertTrue(alias_parent.is_symlink())

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/tmp").is_symlink(),
        "requires the macOS system /tmp alias",
    )
    def test_actual_macos_system_temp_alias_binds_private_tmp(self) -> None:
        workspace_path: Path | None = None

        with MODULE.temporary_archive_workspace(
            prefix="codex-system-tmp-regression.",
            parent=Path("/tmp"),
        ) as workspace:
            workspace_path = workspace.path
            self.assertEqual(workspace.path.parent, Path("/private/tmp"))
            self.assertEqual(stat.S_IMODE(workspace.path.stat().st_mode), 0o700)

        assert workspace_path is not None
        self.assertFalse(workspace_path.exists())

    def test_macos_system_temp_alias_replacement_during_binding_fails_closed(
        self,
    ) -> None:
        canonical_parent = self.root / "replacement-private-tmp"
        alias_parent = self.root / "replacement-tmp"
        canonical_parent.mkdir(mode=0o700)
        canonical_parent = canonical_parent.resolve()
        alias_parent.symlink_to(canonical_parent, target_is_directory=True)
        real_open = MODULE.os.open
        replaced = False

        def replace_alias_before_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal replaced
            if not replaced and Path(path) == canonical_parent and dir_fd is None:
                replaced = True
                alias_parent.unlink()
                alias_parent.symlink_to(canonical_parent, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            mock.patch.object(
                MODULE,
                "_uses_macos_system_temp_alias",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_ALIAS",
                alias_parent,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_DIRECTORY",
                canonical_parent,
            ),
            mock.patch.object(
                MODULE.os,
                "open",
                side_effect=replace_alias_before_open,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "system temporary archive alias changed while binding",
            ),
        ):
            with MODULE.bind_archive_workspace(alias_parent):
                self.fail("a replaced system temporary alias must not be yielded")

        self.assertTrue(replaced)
        self.assertTrue(alias_parent.is_symlink())
        self.assertTrue(canonical_parent.is_dir())

    def test_macos_system_temp_alias_descriptor_closes_before_yield(self) -> None:
        canonical_parent = self.root / "descriptor-private-tmp"
        alias_parent = self.root / "descriptor-tmp"
        canonical_parent.mkdir(mode=0o700)
        canonical_parent = canonical_parent.resolve()
        alias_parent.symlink_to(canonical_parent, target_is_directory=True)
        real_open = MODULE.os.open
        alias_fd = -1

        def capture_alias_fd(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal alias_fd
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path) == alias_parent and dir_fd is None:
                alias_fd = descriptor
            return descriptor

        with (
            mock.patch.object(
                MODULE,
                "_uses_macos_system_temp_alias",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_ALIAS",
                alias_parent,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_DIRECTORY",
                canonical_parent,
            ),
            mock.patch.object(MODULE.os, "open", side_effect=capture_alias_fd),
        ):
            with MODULE.bind_archive_workspace(alias_parent):
                self.assertGreaterEqual(alias_fd, 0)
                with self.assertRaises(OSError):
                    os.fstat(alias_fd)

    def test_macos_system_temp_alias_close_failure_is_reported(self) -> None:
        canonical_parent = self.root / "descriptor-close-private-tmp"
        alias_parent = self.root / "descriptor-close-tmp"
        canonical_parent.mkdir(mode=0o700)
        canonical_parent = canonical_parent.resolve()
        alias_parent.symlink_to(canonical_parent, target_is_directory=True)
        real_open = MODULE.os.open
        real_close = MODULE.os.close
        alias_fd = -1
        close_failed = False

        def capture_alias_fd(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal alias_fd
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path) == alias_parent and dir_fd is None:
                alias_fd = descriptor
            return descriptor

        def fail_alias_close(file_descriptor: int) -> None:
            nonlocal close_failed
            if file_descriptor == alias_fd and not close_failed:
                close_failed = True
                real_close(file_descriptor)
                raise OSError("simulated alias descriptor close failure")
            real_close(file_descriptor)

        with (
            mock.patch.object(
                MODULE,
                "_uses_macos_system_temp_alias",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_ALIAS",
                alias_parent,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_DIRECTORY",
                canonical_parent,
            ),
            mock.patch.object(MODULE.os, "open", side_effect=capture_alias_fd),
            mock.patch.object(MODULE.os, "close", side_effect=fail_alias_close),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "failed to close system temporary archive alias",
            ),
        ):
            with MODULE.bind_archive_workspace(alias_parent):
                self.fail("an alias close failure must prevent workspace use")

        self.assertTrue(close_failed)

    def test_macos_system_temp_alias_close_failure_preserves_primary(self) -> None:
        canonical_parent = self.root / "descriptor-primary-private-tmp"
        unexpected_parent = self.root / "descriptor-primary-unexpected-tmp"
        alias_parent = self.root / "descriptor-primary-tmp"
        canonical_parent.mkdir(mode=0o700)
        unexpected_parent.mkdir(mode=0o700)
        canonical_parent = canonical_parent.resolve()
        unexpected_parent = unexpected_parent.resolve()
        alias_parent.symlink_to(unexpected_parent, target_is_directory=True)
        real_open = MODULE.os.open
        real_close = MODULE.os.close
        alias_fd = -1
        close_failed = False
        stderr = io.StringIO()

        def capture_alias_fd(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal alias_fd
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path) == alias_parent and dir_fd is None:
                alias_fd = descriptor
            return descriptor

        def fail_alias_close(file_descriptor: int) -> None:
            nonlocal close_failed
            if file_descriptor == alias_fd and not close_failed:
                close_failed = True
                real_close(file_descriptor)
                raise OSError("simulated alias descriptor close failure")
            real_close(file_descriptor)

        with (
            mock.patch.object(
                MODULE,
                "_uses_macos_system_temp_alias",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_ALIAS",
                alias_parent,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_DIRECTORY",
                canonical_parent,
            ),
            mock.patch.object(MODULE.os, "open", side_effect=capture_alias_fd),
            mock.patch.object(MODULE.os, "close", side_effect=fail_alias_close),
            contextlib.redirect_stderr(stderr),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "refusing non-standard macOS system temporary alias",
            ),
        ):
            with MODULE.bind_archive_workspace(alias_parent):
                self.fail("the unexpected alias target must not be yielded")

        self.assertTrue(close_failed)
        self.assertIn(
            "warning: failed to close system temporary archive alias",
            stderr.getvalue(),
        )

    def test_macos_system_temp_alias_cleanup_attempts_both_owned_fds(self) -> None:
        canonical_parent = self.root / "descriptor-dual-close-private-tmp"
        alias_parent = self.root / "descriptor-dual-close-tmp"
        canonical_parent.mkdir(mode=0o700)
        canonical_parent = canonical_parent.resolve()
        alias_parent.symlink_to(canonical_parent, target_is_directory=True)
        real_open = MODULE.os.open
        real_close = MODULE.os.close
        alias_fd = -1
        workspace_fd = -1
        closed_fds: list[int] = []
        stderr = io.StringIO()

        def capture_owned_fds(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal alias_fd, workspace_fd
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path) == alias_parent and dir_fd is None:
                alias_fd = descriptor
            elif Path(path) == canonical_parent and dir_fd is None:
                workspace_fd = descriptor
            return descriptor

        def fail_owned_close(file_descriptor: int) -> None:
            if file_descriptor in {alias_fd, workspace_fd}:
                closed_fds.append(file_descriptor)
                real_close(file_descriptor)
                raise OSError(f"simulated close failure for fd {file_descriptor}")
            real_close(file_descriptor)

        with (
            mock.patch.object(
                MODULE,
                "_uses_macos_system_temp_alias",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_ALIAS",
                alias_parent,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_DIRECTORY",
                canonical_parent,
            ),
            mock.patch.object(MODULE.os, "open", side_effect=capture_owned_fds),
            mock.patch.object(MODULE.os, "close", side_effect=fail_owned_close),
            mock.patch.object(
                MODULE,
                "_revalidate_archive_workspace_alias",
                side_effect=MODULE.SyncError("simulated alias revalidation failure"),
            ),
            contextlib.redirect_stderr(stderr),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "simulated alias revalidation failure",
            ),
        ):
            with MODULE.bind_archive_workspace(alias_parent):
                self.fail("failed alias revalidation must not yield the workspace")

        self.assertEqual(set(closed_fds), {alias_fd, workspace_fd})
        for file_descriptor in (alias_fd, workspace_fd):
            with self.assertRaises(OSError):
                os.fstat(file_descriptor)
        cleanup_output = stderr.getvalue()
        self.assertIn("warning: failed to close archive workspace", cleanup_output)
        self.assertIn(
            "warning: failed to close system temporary archive alias",
            cleanup_output,
        )

    def test_macos_system_temp_alias_rejects_unexpected_target(self) -> None:
        canonical_parent = self.root / "expected-private-tmp"
        unexpected_parent = self.root / "unexpected-private-tmp"
        alias_parent = self.root / "unexpected-tmp"
        canonical_parent.mkdir(mode=0o700)
        unexpected_parent.mkdir(mode=0o700)
        canonical_parent = canonical_parent.resolve()
        unexpected_parent = unexpected_parent.resolve()
        alias_parent.symlink_to(unexpected_parent, target_is_directory=True)

        with (
            mock.patch.object(
                MODULE,
                "_uses_macos_system_temp_alias",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_ALIAS",
                alias_parent,
            ),
            mock.patch.object(
                MODULE,
                "MACOS_SYSTEM_TEMP_DIRECTORY",
                canonical_parent,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "refusing non-standard macOS system temporary alias",
            ),
        ):
            with MODULE.bind_archive_workspace(alias_parent):
                self.fail("an unexpected system temporary target must not be yielded")

    def test_archive_workspace_revalidates_access_policy_not_child_churn(
        self,
    ) -> None:
        parent = self.root / "archive-access-policy"
        parent.mkdir(mode=0o700)

        with MODULE.bind_archive_workspace(parent) as workspace:
            child = parent / "benign-child"
            child.write_text(
                "content churn is not parent replacement\n", encoding="utf-8"
            )
            check_fd = MODULE._duplicate_bound_archive_workspace(workspace)
            os.close(check_fd)

            parent.chmod(0o755)
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "archive workspace binding changed",
            ):
                MODULE._duplicate_bound_archive_workspace(workspace)

    def test_arbitrary_archive_workspace_leaf_symlink_remains_rejected(self) -> None:
        canonical_parent = self.root / "arbitrary-real-temp"
        alias_parent = self.root / "arbitrary-temp-alias"
        canonical_parent.mkdir(mode=0o700)
        alias_parent.symlink_to(canonical_parent, target_is_directory=True)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "failed to bind archive workspace",
        ):
            with MODULE.bind_archive_workspace(alias_parent):
                self.fail("an arbitrary leaf symlink must remain rejected")

    def test_temporary_archive_workspace_rejects_leaf_replacement_before_open(
        self,
    ) -> None:
        parent = self.root / "replace-before-open-parent"
        moved_workspace = self.root / "moved-before-open-workspace"
        parent.mkdir()
        real_open = MODULE.os.open
        replaced_path: Path | None = None

        def replace_before_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal replaced_path
            if (
                replaced_path is None
                and dir_fd is not None
                and isinstance(path, str)
                and path.startswith("replace-before-open.")
            ):
                replaced_path = parent / path
                replaced_path.rename(moved_workspace)
                replaced_path.mkdir()
                (replaced_path / "sentinel.txt").write_text(
                    "replacement\n",
                    encoding="utf-8",
                )
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with MODULE.bind_archive_workspace(parent) as parent_workspace:
            with (
                mock.patch.object(
                    MODULE.os,
                    "open",
                    side_effect=replace_before_open,
                ),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "temporary archive workspace changed while binding",
                ),
            ):
                with MODULE.create_bound_temporary_archive_workspace(
                    parent_workspace,
                    prefix="replace-before-open.",
                ):
                    self.fail("replacement workspace must not be yielded")

        assert replaced_path is not None
        self.assertTrue(moved_workspace.is_dir())
        self.assertEqual(
            (replaced_path / "sentinel.txt").read_text(encoding="utf-8"),
            "replacement\n",
        )

    def test_temporary_archive_workspace_open_failure_preserves_replacement(
        self,
    ) -> None:
        parent = self.root / "open-failure-cleanup-parent"
        moved_workspace = self.root / "moved-open-failure-workspace"
        parent.mkdir()
        real_open = MODULE.os.open
        real_rename = MODULE._rename_noreplace_at
        raced = False

        def fail_workspace_open(path, flags, mode=0o777, *, dir_fd=None):
            if (
                dir_fd is not None
                and isinstance(path, str)
                and path.startswith("open-failure-cleanup.")
            ):
                raise OSError("simulated workspace open failure")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def replace_before_isolation(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        ):
            nonlocal raced
            if not raced:
                raced = True
                workspace_path = parent / source_name
                workspace_path.rename(moved_workspace)
                workspace_path.mkdir()
                (workspace_path / "sentinel.txt").write_text(
                    "replacement\n",
                    encoding="utf-8",
                )
            return real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        stderr = io.StringIO()
        with MODULE.bind_archive_workspace(parent) as parent_workspace:
            with (
                mock.patch.object(
                    MODULE.os,
                    "open",
                    side_effect=fail_workspace_open,
                ),
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=replace_before_isolation,
                ),
                contextlib.redirect_stderr(stderr),
                self.assertRaisesRegex(
                    OSError,
                    "simulated workspace open failure",
                ),
            ):
                with MODULE.create_bound_temporary_archive_workspace(
                    parent_workspace,
                    prefix="open-failure-cleanup.",
                ):
                    self.fail("workspace open failure must not yield")

        self.assertTrue(raced)
        self.assertTrue(moved_workspace.is_dir())
        isolated = list(parent.glob(".codex-archive-cleanup-*"))
        self.assertEqual(len(isolated), 1)
        self.assertEqual(
            (isolated[0] / "sentinel.txt").read_text(encoding="utf-8"),
            "replacement\n",
        )
        self.assertIn("changed during cleanup isolation", stderr.getvalue())

    def test_bind_archive_workspace_close_failure_preserves_primary(self) -> None:
        parent = self.root / "bind-close-primary"
        parent.mkdir()
        real_close = MODULE.os.close
        workspace_fd = -1
        close_failed = False
        stderr = io.StringIO()

        def fail_workspace_close(file_descriptor: int) -> None:
            nonlocal close_failed
            if file_descriptor == workspace_fd and not close_failed:
                close_failed = True
                raise OSError("simulated workspace close failure")
            real_close(file_descriptor)

        try:
            with (
                mock.patch.object(
                    MODULE.os,
                    "close",
                    side_effect=fail_workspace_close,
                ),
                contextlib.redirect_stderr(stderr),
                self.assertRaisesRegex(RuntimeError, "primary failure"),
            ):
                with MODULE.bind_archive_workspace(parent) as workspace:
                    workspace_fd = workspace.fd
                    raise RuntimeError("primary failure")
        finally:
            if workspace_fd >= 0:
                real_close(workspace_fd)

        self.assertTrue(close_failed)
        self.assertIn(
            "warning: failed to close archive workspace",
            stderr.getvalue(),
        )

    def test_temporary_archive_child_close_failure_is_reported(self) -> None:
        parent = self.root / "child-close-report-parent"
        parent.mkdir()
        real_close = MODULE.os.close
        child_identity: tuple[int, int] | None = None
        failed_fd = -1

        def fail_child_close(file_descriptor: int) -> None:
            nonlocal failed_fd
            try:
                metadata = os.fstat(file_descriptor)
            except OSError:
                real_close(file_descriptor)
                return
            if (
                child_identity is not None
                and (metadata.st_dev, metadata.st_ino) == child_identity
                and failed_fd < 0
            ):
                failed_fd = file_descriptor
                raise OSError("simulated child close failure")
            real_close(file_descriptor)

        try:
            with (
                mock.patch.object(
                    MODULE.os,
                    "close",
                    side_effect=fail_child_close,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "failed to close temporary archive workspace entry",
                ),
            ):
                with MODULE.temporary_archive_workspace(
                    prefix="child-close-report.",
                    parent=parent,
                ) as workspace:
                    child = workspace.path / "child"
                    child.mkdir()
                    metadata = child.stat()
                    child_identity = (metadata.st_dev, metadata.st_ino)
        finally:
            if failed_fd >= 0:
                real_close(failed_fd)

        self.assertGreaterEqual(failed_fd, 0)

    def test_temporary_archive_child_close_failure_preserves_primary(
        self,
    ) -> None:
        parent = self.root / "child-close-primary-parent"
        parent.mkdir()
        real_close = MODULE.os.close
        child_identity: tuple[int, int] | None = None
        failed_fd = -1
        stderr = io.StringIO()

        def fail_child_close(file_descriptor: int) -> None:
            nonlocal failed_fd
            try:
                metadata = os.fstat(file_descriptor)
            except OSError:
                real_close(file_descriptor)
                return
            if (
                child_identity is not None
                and (metadata.st_dev, metadata.st_ino) == child_identity
                and failed_fd < 0
            ):
                failed_fd = file_descriptor
                raise OSError("simulated child close failure")
            real_close(file_descriptor)

        try:
            with (
                mock.patch.object(
                    MODULE.os,
                    "close",
                    side_effect=fail_child_close,
                ),
                contextlib.redirect_stderr(stderr),
                self.assertRaisesRegex(RuntimeError, "primary failure"),
            ):
                with MODULE.temporary_archive_workspace(
                    prefix="child-close-primary.",
                    parent=parent,
                ) as workspace:
                    child = workspace.path / "child"
                    child.mkdir()
                    metadata = child.stat()
                    child_identity = (metadata.st_dev, metadata.st_ino)
                    raise RuntimeError("primary failure")
        finally:
            if failed_fd >= 0:
                real_close(failed_fd)

        self.assertGreaterEqual(failed_fd, 0)
        self.assertIn(
            "warning: failed to close temporary archive workspace entry",
            stderr.getvalue(),
        )

    def test_temporary_archive_cleanup_preserves_root_replacement(self) -> None:
        parent = self.root / "cleanup-root-parent"
        moved_workspace = self.root / "moved-cleanup-root"
        replacement = self.root / "replacement-cleanup-root"
        parent.mkdir()
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        real_rename = MODULE._rename_noreplace_at
        replaced = False

        def replace_root(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        ):
            nonlocal replaced
            if source_name.startswith("cleanup-root.") and not replaced:
                replaced = True
                (parent / source_name).rename(moved_workspace)
                replacement.rename(parent / source_name)
            return real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=replace_root,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed during cleanup isolation",
            ),
        ):
            with MODULE.temporary_archive_workspace(
                prefix="cleanup-root.",
                parent=parent,
            ) as workspace:
                (workspace.path / "owned.txt").write_text(
                    "owned\n",
                    encoding="utf-8",
                )

        self.assertTrue(replaced)
        self.assertEqual(
            (moved_workspace / "owned.txt").read_text(encoding="utf-8"),
            "owned\n",
        )
        retained_replacements = list(parent.glob(".codex-archive-cleanup-*"))
        self.assertEqual(len(retained_replacements), 1)
        self.assertEqual(
            (retained_replacements[0] / "sentinel.txt").read_text(encoding="utf-8"),
            "keep\n",
        )

    def test_temporary_archive_cleanup_preserves_leaf_replacement_and_primary(
        self,
    ) -> None:
        parent = self.root / "cleanup-leaf-parent"
        parent.mkdir()
        real_rename = MODULE._rename_noreplace_at
        replaced = False
        stderr = io.StringIO()

        def replace_leaf(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        ):
            nonlocal replaced
            if source_name == "owned.txt" and not replaced:
                replaced = True
                os.rename(
                    source_name,
                    "moved-owned.txt",
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=source_parent_fd,
                )
                replacement_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(replacement_fd, b"replacement\n")
                finally:
                    os.close(replacement_fd)
            return real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=replace_leaf,
            ),
            contextlib.redirect_stderr(stderr),
            self.assertRaisesRegex(RuntimeError, "primary failure"),
        ):
            with MODULE.temporary_archive_workspace(
                prefix="cleanup-leaf.",
                parent=parent,
            ) as workspace:
                (workspace.path / "owned.txt").write_text(
                    "owned\n",
                    encoding="utf-8",
                )
                raise RuntimeError("primary failure")

        self.assertTrue(replaced)
        self.assertIn("warning:", stderr.getvalue())
        retained_roots = list(parent.glob(".codex-archive-cleanup-*"))
        self.assertEqual(len(retained_roots), 1)
        retained_root = retained_roots[0]
        self.assertEqual(
            (retained_root / "moved-owned.txt").read_text(encoding="utf-8"),
            "owned\n",
        )
        retained_replacements = [
            path
            for path in retained_root.glob(".codex-archive-cleanup-*")
            if path.is_file()
        ]
        self.assertEqual(len(retained_replacements), 1)
        self.assertEqual(
            retained_replacements[0].read_text(encoding="utf-8"),
            "replacement\n",
        )

    def test_safe_extract_accepts_unresolved_temporary_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="archive-workspace-no-resolve."
        ) as temp_dir_raw:
            workspace_path = Path(temp_dir_raw)
            source_root = workspace_path / "source"
            archive_path = workspace_path / "release.tar.gz"
            write_minimal_release(source_root)
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(source_root, arcname=f"personal-codex-{SHA1}")

            with MODULE.bind_archive_workspace(workspace_path) as workspace:
                release_root = MODULE.safe_extract_archive(
                    archive_path,
                    workspace_path / "extract",
                    workspace=workspace,
                )

            self.assertTrue(
                (release_root / "personal_codex" / "sync-manifest.json").is_file()
            )

    def test_safe_extract_rejects_replaced_workspace_path(self) -> None:
        workspace_path = self.root / "replaceable-workspace"
        retained_workspace = self.root / "retained-workspace"
        replacement_marker = workspace_path / "replacement.txt"
        source_root = self.root / "workspace-replacement-source"
        archive_path = self.root / "workspace-replacement.tar.gz"
        workspace_path.mkdir()
        write_minimal_release(source_root)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")

        with MODULE.bind_archive_workspace(workspace_path) as workspace:
            workspace_path.rename(retained_workspace)
            workspace_path.mkdir()
            replacement_marker.write_text("replacement\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "archive workspace binding changed",
            ):
                MODULE.safe_extract_archive(
                    archive_path,
                    workspace_path / "extract",
                    workspace=workspace,
                )

        self.assertEqual(
            replacement_marker.read_text(encoding="utf-8"), "replacement\n"
        )
        self.assertEqual(list(retained_workspace.iterdir()), [])

    def test_safe_extract_rejects_closed_or_mismatched_workspace_fd(self) -> None:
        source_root = self.root / "closed-workspace-source"
        archive_path = self.root / "closed-workspace.tar.gz"
        first = self.root / "first-workspace"
        second = self.root / "second-workspace"
        first.mkdir()
        second.mkdir()
        write_minimal_release(source_root)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")

        with MODULE.bind_archive_workspace(first) as closed_workspace:
            pass
        with self.assertRaisesRegex(MODULE.SyncError, "no longer bound"):
            MODULE.safe_extract_archive(
                archive_path,
                first / "extract",
                workspace=closed_workspace,
            )

        with (
            MODULE.bind_archive_workspace(first) as first_workspace,
            MODULE.bind_archive_workspace(second) as second_workspace,
        ):
            mismatched_workspace = MODULE.BoundArchiveWorkspace(
                path=first_workspace.path,
                fd=second_workspace.fd,
                identity=first_workspace.identity,
                access_policy=first_workspace.access_policy,
            )
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "archive workspace binding changed",
            ):
                MODULE.safe_extract_archive(
                    archive_path,
                    first / "extract",
                    workspace=mismatched_workspace,
                )

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        archive_path = self.root / "unsafe.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            data = b"bad"
            member = tarfile.TarInfo("../evil.txt")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

        with self.assertRaisesRegex(MODULE.SyncError, "unsafe archive member path"):
            self.safe_extract_archive(archive_path, self.root / "extract")
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
                        self.safe_extract_archive(archive_path, destination)
                self.assertFalse(destination.exists())

    def test_release_tree_snapshot_enforces_archive_depth_boundary(self) -> None:
        release_root = self.root / "archive-depth-release-tree"
        manifest = release_root / MODULE.MANIFEST_RELATIVE_PATH
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}\n", encoding="utf-8")
        current = release_root
        for _ in range(MODULE.MAX_ARCHIVE_MEMBER_PATH_DEPTH - 1):
            current /= "d"
            current.mkdir()

        self.snapshot_release_tree(release_root)

        current /= "d"
        current.mkdir()
        with self.assertRaisesRegex(MODULE.SyncError, "path exceeds depth limit"):
            self.snapshot_release_tree(release_root)

    def test_release_tree_path_validation_uses_archive_utf8_byte_boundary(
        self,
    ) -> None:
        relative_root = MODULE.PurePosixPath("d")
        child_name = "\N{CJK UNIFIED IDEOGRAPH-754C}"
        archive_member_name = (
            f"{MODULE.CANONICAL_PACKAGE_ROOT_COMPONENT}/"
            f"{relative_root.as_posix()}/{child_name}"
        )
        boundary = len(archive_member_name.encode("utf-8"))

        with mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBER_PATH_BYTES", boundary):
            self.assertEqual(
                MODULE._validated_release_tree_child_path(
                    relative_root,
                    child_name,
                ),
                relative_root / child_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "MAX_ARCHIVE_MEMBER_PATH_BYTES",
                boundary - 1,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "path exceeds UTF-8 byte limit"),
        ):
            MODULE._validated_release_tree_child_path(relative_root, child_name)

    def test_release_tree_snapshot_enforces_component_byte_limit(self) -> None:
        release_root = self.root / "component-limit-release-tree"
        release_root.mkdir()
        component_limit = len(MODULE.CANONICAL_PACKAGE_ROOT_COMPONENT.encode("utf-8"))
        (release_root / ("w" * (component_limit + 1))).write_bytes(b"x")

        with (
            mock.patch.object(
                MODULE,
                "MAX_ARCHIVE_MEMBER_COMPONENT_BYTES",
                component_limit,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "path component exceeds UTF-8 byte limit",
            ),
        ):
            self.snapshot_release_tree(release_root)

    def test_release_tree_snapshot_rejects_too_many_entries_before_hashing(
        self,
    ) -> None:
        release_root = self.root / "many-entry-release-tree"
        release_root.mkdir()
        nested = release_root / "a"
        nested.mkdir()
        (nested / "a").write_bytes(b"a")
        (nested / "b").write_bytes(b"b")
        (release_root / "b").write_bytes(b"b")

        with (
            mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBERS", 4),
            mock.patch.object(
                MODULE,
                "_hash_exact_regular_file",
                side_effect=AssertionError("file hashed after entry limit"),
            ) as hash_file,
            self.assertRaisesRegex(MODULE.SyncError, "path entry limit"),
        ):
            self.snapshot_release_tree(release_root)

        hash_file.assert_not_called()

    def test_release_tree_snapshot_rejects_oversized_file_before_hashing(
        self,
    ) -> None:
        release_root = self.root / "oversized-release-tree"
        release_root.mkdir()
        (release_root / "payload.bin").write_bytes(b"long")

        with (
            mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBER_BYTES", 3),
            mock.patch.object(
                MODULE,
                "_hash_exact_regular_file",
                side_effect=AssertionError("oversized file hashed"),
            ) as hash_file,
            self.assertRaisesRegex(
                MODULE.SyncError, "file exceeds expanded byte limit"
            ),
        ):
            self.snapshot_release_tree(release_root)

        hash_file.assert_not_called()

    def test_release_tree_snapshot_rejects_aggregate_size_before_next_hash(
        self,
    ) -> None:
        release_root = self.root / "aggregate-release-tree"
        release_root.mkdir()
        (release_root / "a").write_bytes(b"abc")
        (release_root / "b").write_bytes(b"def")
        real_hash = MODULE._hash_exact_regular_file

        with (
            mock.patch.object(MODULE, "MAX_ARCHIVE_MEMBER_BYTES", 10),
            mock.patch.object(MODULE, "MAX_ARCHIVE_EXPANDED_BYTES", 5),
            mock.patch.object(
                MODULE,
                "_hash_exact_regular_file",
                wraps=real_hash,
            ) as hash_file,
            self.assertRaisesRegex(MODULE.SyncError, "total expanded byte limit"),
        ):
            self.snapshot_release_tree(release_root)

        self.assertEqual(hash_file.call_count, 1)

    def test_copy_tree_rejects_growth_after_identity_before_next_copy(self) -> None:
        release_root = self.root / "growing-release-tree"
        destination = self.root / "growing-release-destination"
        write_minimal_release(release_root)
        MODULE._source_release_identity(release_root, None)
        baseline_files = tuple(
            path for path in release_root.rglob("*") if path.is_file()
        )
        baseline_bytes = sum(path.stat().st_size for path in baseline_files)
        (release_root / "zz-growth.bin").write_bytes(b"abc")
        destination.mkdir()
        source_fd = os.open(release_root, MODULE._source_directory_flags())
        destination_fd = os.open(destination, MODULE._source_directory_flags())
        real_copy = MODULE._copy_bytes
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "MAX_ARCHIVE_EXPANDED_BYTES",
                    baseline_bytes + 2,
                ),
                mock.patch.object(
                    MODULE,
                    "_copy_bytes",
                    wraps=real_copy,
                ) as copy_bytes,
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "total expanded byte limit",
                ),
            ):
                MODULE._copy_tree_from_directory_fd(
                    source_fd,
                    destination_fd,
                    release_root,
                    MODULE.PurePosixPath(),
                    {},
                    {},
                )
        finally:
            os.close(destination_fd)
            os.close(source_fd)

        self.assertEqual(copy_bytes.call_count, len(baseline_files))
        self.assertFalse((destination / "zz-growth.bin").exists())

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
                self.safe_extract_archive(archive_path, destination)

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
        archive_path.write_bytes(archive_payload + MODULE.gzip.compress(b"x" * 4096))
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
                self.safe_extract_archive(archive_path, destination)

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
            with self.assertRaisesRegex(
                MODULE.SyncError, "file changed during validation"
            ):
                self.safe_extract_archive(archive_path, destination)

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
                    self.safe_extract_archive(archive_path, destination)
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
            self.safe_extract_archive(archive_path, destination)

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
                        self.safe_extract_archive(archive_path, destination)
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
            self.safe_extract_archive(archive_path, destination)

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
            self.safe_extract_archive(archive_path, destination)

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
                    self.safe_extract_archive(archive_path, destination)

                self.assertFalse(destination.exists())

    def test_safe_extract_rejects_hardlink_member(self) -> None:
        archive_path = self.root / "hardlink.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("personal-codex/link")
            member.type = tarfile.LNKTYPE
            member.linkname = "target"
            archive.addfile(member)

        with self.assertRaisesRegex(MODULE.SyncError, "archive link member"):
            self.safe_extract_archive(archive_path, self.root / "extract")

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
            self.safe_extract_archive(archive_path, destination)

        self.assertTrue(destination.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_safe_extract_rejects_symlink_destination_ancestor(self) -> None:
        source_root = self.root / "ancestor-source"
        write_minimal_release(source_root)
        archive_path = self.root / "ancestor.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_root, arcname=f"personal-codex-{SHA1}")
        outside = self.root / "ancestor-outside"
        nested_outside = outside / "nested"
        nested_outside.mkdir(parents=True)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        linked_ancestor = self.root / "linked-ancestor"
        linked_ancestor.symlink_to(outside, target_is_directory=True)
        destination = linked_ancestor / "nested" / "extract"

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsafe archive directory",
        ):
            self.safe_extract_archive(archive_path, destination)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(list(nested_outside.iterdir()), [])
        self.assertFalse(destination.exists())

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
                self.safe_extract_archive(archive_path, destination)

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

    def test_safe_extract_parent_swap_does_not_create_in_redirected_parent(
        self,
    ) -> None:
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
                self.safe_extract_archive(archive_path, destination)

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
                    with self.assertRaisesRegex(
                        MODULE.SyncError, "entry already exists"
                    ):
                        self.safe_extract_archive(archive_path, destination)

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
        release_root = self.safe_extract_archive(archive_path, destination)

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
            release_root = self.safe_extract_archive(
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
                self.safe_extract_archive(archive_path, destination)

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
            release_root = self.safe_extract_archive(
                archive_path, self.root / "extract"
            )

        extractall.assert_not_called()
        mode = (release_root / "personal_codex" / "bin" / "example-tool").stat().st_mode
        self.assertEqual(mode & 0o7000, 0)
        self.assertEqual(mode & 0o022, 0)
        self.assertTrue(mode & 0o100)

    def test_load_manifest_requires_skill_markdown(self) -> None:
        release_root = self.root / "release"
        write_minimal_release(release_root)
        (
            release_root / "personal_codex" / "skills" / "example-skill" / "SKILL.md"
        ).unlink()

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

    def test_manifest_rejects_release_retention_control_targets(self) -> None:
        def payload(route: str, target: str) -> dict[str, object]:
            active_link: dict[str, object] = {
                "source": "personal_codex/AGENTS.md",
                "target": "AGENTS.md",
                "kind": "file",
            }
            data: dict[str, object] = {
                "version": 1,
                "links": [active_link],
            }
            if route == "active":
                active_link["target"] = target
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

        for reserved_target in MODULE.RELEASE_RETENTION_CONTROL_TARGETS:
            exact = reserved_target.as_posix()
            portable_alias = exact.upper()
            variants = (
                ("exact", exact),
                ("portable-alias", portable_alias),
                ("descendant", f"{exact}/child"),
                ("portable-alias-descendant", f"{portable_alias}/child"),
            )
            for route in ("active", "removed", "replacement"):
                for variant, target in variants:
                    with (
                        self.subTest(
                            reserved=exact,
                            route=route,
                            variant=variant,
                        ),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "release retention transaction/control path",
                        ),
                    ):
                        MODULE._parse_manifest_data(
                            payload(route, target),
                            lambda _path: "file",
                        )

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

    def test_manifest_relative_paths_reject_backslashes(self) -> None:
        for field_name, raw_path in (
            ("source", "personal_codex\\AGENTS.md"),
            ("target", "skills\\example"),
        ):
            with (
                self.subTest(field=field_name),
                self.assertRaisesRegex(MODULE.SyncError, "safe POSIX"),
            ):
                MODULE._validate_relative_path(raw_path, field_name)

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
            self.run_quietly(
                MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
            )

    def test_install_release_tree_recovers_when_release_dir_already_exists(
        self,
    ) -> None:
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
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )

        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "agent\n")

    def test_doctor_classifies_unexpected_release_cache_as_immutable_drift(
        self,
    ) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree,
            release_root,
            home,
            SHA1,
            dry_run=False,
        )
        installed = home / "personal-sync" / "releases" / SHA1
        cache = installed / "review_runtime" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "guard.cpython-313.pyc").write_bytes(b"pyc-313")
        (cache / "guard.cpython-314.pyc").write_bytes(b"pyc-314")
        release_identity = installed.stat().st_dev, installed.stat().st_ino

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "immutable release drift: unexpected cache artifacts",
        ) as raised:
            self.run_quietly(
                MODULE.install_release_tree,
                release_root,
                home,
                SHA1,
                dry_run=False,
            )

        self.assertEqual(raised.exception.code, "immutable-release-drift")
        self.assertEqual(
            (installed.stat().st_dev, installed.stat().st_ino),
            release_identity,
        )
        self.assertEqual((cache / "guard.cpython-313.pyc").read_bytes(), b"pyc-313")
        self.assertEqual((cache / "guard.cpython-314.pyc").read_bytes(), b"pyc-314")
        with contextlib.redirect_stdout(io.StringIO()):
            _report, issues = MODULE.doctor(
                home,
                "linux",
                json_output=True,
            )
        drift = [issue for issue in issues if issue.code == "immutable-release-drift"]
        self.assertTrue(drift)
        self.assertTrue(
            any("unexpected cache artifacts" in issue.detail for issue in drift)
        )

    def test_install_rejects_source_release_cache_artifacts(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        cache = release_root / "review_runtime" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "guard.cpython-314.pyc").write_bytes(b"generated")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "release package contains unexpected cache artifacts",
        ):
            self.run_quietly(
                MODULE.install_release_tree,
                release_root,
                home,
                SHA1,
                dry_run=False,
            )

        self.assertFalse((home / "personal-sync").exists())

    def test_scheduler_release_baseline_detects_bytes_not_mtime(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree,
            release_root,
            home,
            SHA1,
            dry_run=False,
        )
        baseline = MODULE._capture_scheduler_release_trees(
            home,
            mode="public",
            owner=MODULE.PUBLIC_OWNER,
        )
        installed_agent = (
            home / "personal-sync" / "releases" / SHA1 / "personal_codex" / "AGENTS.md"
        )
        metadata = installed_agent.stat()
        os.utime(
            installed_agent,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
        )
        self.assertEqual(
            MODULE._scheduler_release_integrity_issues(
                home,
                ((MODULE.PUBLIC_OWNER, SHA1),),
                baseline,
            ),
            (),
        )

        installed_agent.write_text("other\n", encoding="utf-8")
        issues = MODULE._scheduler_release_integrity_issues(
            home,
            ((MODULE.PUBLIC_OWNER, SHA1),),
            baseline,
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "immutable-release-drift")
        self.assertIn("differs from the last verified", issues[0][3])

    def test_install_release_tree_removes_stale_links_after_manifest_shrink(
        self,
    ) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        (home / "skills" / ".system").mkdir(parents=True)
        (home / "skills" / "host-local").mkdir()
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )

        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )

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
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
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
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_install_release_tree_preserves_existing_local_agents_file(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root, agent_text="public\n")
        home.mkdir(parents=True)
        (home / "AGENTS.md").write_text("local\n", encoding="utf-8")

        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )

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

        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").readlink(), local_agents)
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "local\n")
        self.assertTrue((home / "bin" / "example-tool").is_symlink())

    def test_install_relinquishes_established_optional_agents_claim(self) -> None:
        cases = (
            ("file-public-optional", "file", True),
            ("symlink-reference-only", "symlink", False),
        )
        for case, foreign_kind, next_declares_agents in cases:
            with self.subTest(case=case):
                home = self.root / case / "home" / ".codex"
                release_one = self.root / case / "release-one"
                release_two = self.root / case / "release-two"
                write_agent_only_release(release_one, agent_text="one\n")
                if next_declares_agents:
                    write_agent_only_release(release_two, agent_text="two\n")
                else:
                    write_reference_only_agent_release(
                        release_two,
                        agent_text="two\n",
                    )
                self.run_quietly(
                    MODULE.install_release_tree,
                    release_one,
                    home,
                    SHA1,
                    dry_run=False,
                )
                agents = home / "AGENTS.md"
                agents.unlink()
                if foreign_kind == "file":
                    agents.write_text("local\n", encoding="utf-8")
                else:
                    dotfiles = self.root / case / "dotfiles"
                    dotfiles.mkdir()
                    local_agents = dotfiles / "AGENTS.md"
                    local_agents.write_text("local\n", encoding="utf-8")
                    agents.symlink_to(local_agents)
                foreign_before = foreign_leaf_snapshot(agents)

                self.run_quietly(
                    MODULE.install_release_tree,
                    release_two,
                    home,
                    SHA2,
                    dry_run=False,
                )

                self.assertEqual(current_target(home), f"releases/{SHA2}")
                self.assertEqual(foreign_leaf_snapshot(agents), foreign_before)
                state = json.loads(MODULE._state_path(home).read_text(encoding="utf-8"))
                self.assertEqual(state["owners"], {"public": SHA2})
                self.assertNotIn(
                    "AGENTS.md",
                    {entry["target"] for entry in state["links"]},
                )
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(home))
                )

    def test_install_repairs_missing_established_optional_agents_claim(self) -> None:
        home = self.root / "missing-optional" / "home" / ".codex"
        release_one = self.root / "missing-optional" / "release-one"
        release_two = self.root / "missing-optional" / "release-two"
        write_agent_only_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        self.run_quietly(
            MODULE.install_release_tree,
            release_one,
            home,
            SHA1,
            dry_run=False,
        )
        (home / "AGENTS.md").unlink()

        self.run_quietly(
            MODULE.install_release_tree,
            release_two,
            home,
            SHA2,
            dry_run=False,
        )

        agents = home / "AGENTS.md"
        self.assertTrue(agents.is_symlink())
        self.assertEqual(agents.read_text(encoding="utf-8"), "two\n")
        state = json.loads(MODULE._state_path(home).read_text(encoding="utf-8"))
        agents_record = next(
            entry for entry in state["links"] if entry["target"] == "AGENTS.md"
        )
        self.assertEqual(agents_record["release_sha"], SHA2)

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

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "hierarchy changes are not supported",
        ):
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

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "hierarchy changes are not supported",
        ):
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
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "hierarchy changes are not supported",
        ):
            self.run_quietly(
                MODULE.install_release_tree,
                release_two,
                fresh_home,
                SHA2,
                dry_run=False,
            )
        self.assertFalse((fresh_home / "personal-sync").exists())

    def test_install_release_tree_rejects_tampered_managed_state(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
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

    def test_install_release_tree_bootstraps_historical_manifest_ownership(
        self,
    ) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one)
        write_agent_only_release(release_two)
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )
        state_path = home / "personal-sync" / "state" / "managed-links.json"
        state_path.unlink()
        historical_link = home / "skills" / "example-skill"
        historical_link.symlink_to(
            "../personal-sync/current/personal_codex/skills/example-skill",
            target_is_directory=True,
        )

        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )

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
            list((home / "personal-sync" / "quarantine").glob("*/links/skills/stable")),
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

    def test_removed_links_retire_legacy_workflow_skills_from_active_install(
        self,
    ) -> None:
        home = self.root / "home" / ".codex"
        old_release = self.root / "old-public-workflows"
        new_release = self.root / "new-public-workflows"
        retired_skills = (
            "agile-delivery-workflow",
            "bug-triage-playbook",
            "waited-delivery",
        )
        write_skill_manifest_release(
            old_release,
            skills=("kept-skill", *retired_skills),
        )
        self.run_quietly(
            MODULE.install_release_tree,
            old_release,
            home,
            SHA1,
            dry_run=False,
        )
        write_skill_manifest_release(
            new_release,
            skills=("kept-skill",),
            removed_links=[
                {
                    "id": f"retire-{skill}",
                    "source": f"personal_codex/skills/{skill}",
                    "target": f"skills/{skill}",
                    "kind": "skill",
                    "legacy": True,
                }
                for skill in retired_skills
            ],
            extra_skill_dirs=retired_skills,
        )

        self.run_quietly(
            MODULE.install_release_tree,
            new_release,
            home,
            SHA2,
            dry_run=False,
        )

        self.assertTrue((home / "skills" / "kept-skill").is_symlink())
        for skill in retired_skills:
            self.assertFalse(os.path.lexists(home / "skills" / skill))
        state = MODULE._load_managed_state(home)
        self.assertFalse(
            {f"skills/{skill}" for skill in retired_skills}
            & {target.as_posix() for target in state.links}
        )

    def test_install_private_keeps_legacy_agent_overlay_without_override(self) -> None:
        public_release = self.root / "public-release"
        private_release = self.root / "private-release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(public_release, agent_text="public\n")
        write_private_agent_release(private_release, agent_text="private\n")

        def fake_download(
            repo: str,
            destination: Path,
            *,
            workspace,
            sha: str | None = None,
        ):
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
            (home / "personal-sync" / "overlays" / "private" / "current")
            .readlink()
            .as_posix(),
            f"releases/{SHA2}",
        )
        self.assertTrue((home / "AGENTS.md").is_symlink())
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "private\n")
        self.run_quietly(MODULE.verify_overlay, home, "private")

    def test_dry_run_does_not_mutate_home(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)

        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=True
        )

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
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )

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

    def test_rollback_relinquishes_established_optional_agents_claim(self) -> None:
        release_one = self.root / "rollback-optional-one"
        release_two = self.root / "rollback-optional-two"
        dotfiles = self.root / "rollback-optional-dotfiles"
        home = self.root / "rollback-optional-home" / ".codex"
        write_agent_only_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        self.run_quietly(
            MODULE.install_release_tree,
            release_one,
            home,
            SHA1,
            dry_run=False,
        )
        self.run_quietly(
            MODULE.install_release_tree,
            release_two,
            home,
            SHA2,
            dry_run=False,
        )
        agents = home / "AGENTS.md"
        agents.unlink()
        dotfiles.mkdir()
        local_agents = dotfiles / "AGENTS.md"
        local_agents.write_text("local\n", encoding="utf-8")
        agents.symlink_to(local_agents)
        foreign_before = foreign_leaf_snapshot(agents)

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual(foreign_leaf_snapshot(agents), foreign_before)
        state = json.loads(MODULE._state_path(home).read_text(encoding="utf-8"))
        self.assertEqual(state["owners"], {"public": SHA1})
        self.assertNotIn(
            "AGENTS.md",
            {entry["target"] for entry in state["links"]},
        )
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_rollback_removes_stale_links_after_manifest_shrink(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )

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
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )
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
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_rollback_without_target_uses_most_recent_non_current_release(self) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )

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
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_three, home, SHA3, dry_run=False
        )
        os.utime(home / "personal-sync" / "releases" / SHA1, (300, 300))
        os.utime(home / "personal-sync" / "releases" / SHA2, (200, 200))
        os.utime(home / "personal-sync" / "releases" / SHA3, (100, 100))

        self.run_quietly(MODULE.rollback, home, None)

        self.assertEqual(current_target(home), f"releases/{SHA1}")

    def test_rollback_without_target_ignores_incomplete_release_directories(
        self,
    ) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_minimal_release(release_two, agent_text="two\n")
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )
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
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
        (home / "personal-sync" / "releases" / SHA3).mkdir()

        with self.assertRaisesRegex(MODULE.SyncError, f"no release matches {SHA3[:8]}"):
            self.run_quietly(MODULE.rollback, home, SHA3[:8])

    def test_rollback_to_current_release_repairs_symlink_drift(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
        (home / "AGENTS.md").unlink()

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "AGENTS.md").is_symlink())
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "agent\n")

    def test_rollback_to_current_release_repairs_missing_current_pointer(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
        current = MODULE._current_link(home)
        current.unlink()

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "agent\n")

    def test_rollback_to_current_release_preserves_unmanaged_current_symlink(
        self,
    ) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
        unmanaged_link = home / "bin" / "local-tool"
        unmanaged_link.parent.mkdir(parents=True, exist_ok=True)
        unmanaged_link.symlink_to(
            "../personal-sync/current/personal_codex/bin/local-tool"
        )

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertTrue(unmanaged_link.is_symlink())

    def test_rollback_to_current_release_ignores_incomplete_tmp_manifest_targets(
        self,
    ) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
        tmp_release = home / "personal-sync" / "releases" / f".tmp-{SHA2}-123"
        write_minimal_release(tmp_release)
        unmanaged_link = home / "bin" / "example-tool"
        unmanaged_link.parent.mkdir(parents=True, exist_ok=True)
        unmanaged_link.symlink_to(
            "../personal-sync/current/personal_codex/bin/example-tool"
        )

        self.run_quietly(MODULE.rollback, home, SHA1[:8])

        self.assertTrue(unmanaged_link.is_symlink())

    def test_rollback_to_current_release_preserves_known_target_with_unmanaged_link(
        self,
    ) -> None:
        release_one = self.root / "release-one"
        release_two = self.root / "release-two"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_one, agent_text="one\n")
        write_agent_only_release(release_two, agent_text="two\n")
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )
        unmanaged_link = home / "bin" / "example-tool"
        unmanaged_link.symlink_to(
            "../personal-sync/current/personal_codex/bin/local-tool"
        )

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

    def test_status_reports_unmanaged_broken_skill_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
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
        self.assertIn("broken-link", status_output)
        self.assertIn(str(stale_target), status_output)

    def test_status_ignores_near_miss_current_symlink_substring(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_agent_only_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
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
        self.run_quietly(
            MODULE.install_release_tree, release_one, home, SHA1, dry_run=False
        )
        self.run_quietly(
            MODULE.install_release_tree, release_two, home, SHA2, dry_run=False
        )
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
                        "immutable": True,
                        "assets": [
                            github_release_asset(
                                101,
                                f"personal-codex-{SHA1}.tar.gz",
                            ),
                            github_release_asset(
                                102,
                                f"personal-codex-{SHA1}.sha256",
                            ),
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

    def test_find_latest_release_rejects_mutable_release_without_fallback(self) -> None:
        mutable = {
            "tag_name": "personal-codex-20260511-120000-1111111",
            "target_commitish": SHA1,
            "immutable": False,
            "assets": [
                github_release_asset(101, f"personal-codex-{SHA1}.tar.gz"),
                github_release_asset(102, f"personal-codex-{SHA1}.sha256"),
            ],
        }
        older_immutable = {
            "tag_name": "personal-codex-20260510-120000-2222222",
            "target_commitish": SHA2,
            "immutable": True,
            "assets": [
                github_release_asset(201, f"personal-codex-{SHA2}.tar.gz"),
                github_release_asset(202, f"personal-codex-{SHA2}.sha256"),
            ],
        }
        with (
            mock.patch.object(
                MODULE,
                "_run_gh_json_stream",
                return_value=[[mutable, older_immutable]],
            ),
            self.assertRaisesRegex(MODULE.SyncError, "not immutable"),
        ):
            MODULE.find_latest_release("owner/repo")

        with (
            mock.patch.object(
                MODULE,
                "_run_gh_json_stream",
                return_value=[[mutable]],
            ),
            self.assertRaisesRegex(MODULE.SyncError, "not immutable"),
        ):
            MODULE.find_release_by_asset_sha("owner/repo", SHA1)

        with mock.patch.object(
            MODULE,
            "_run_gh_json_stream",
            return_value=[[mutable]],
        ):
            selected = MODULE.find_latest_release(
                "owner/repo",
                require_immutable=False,
            )
        self.assertEqual(selected["targetCommitish"], SHA1)

    def test_find_latest_release_rejects_missing_release(self) -> None:
        with mock.patch.object(MODULE, "_run_gh_json_stream", return_value=[[]]):
            with self.assertRaisesRegex(MODULE.SyncError, "no personal-codex- release"):
                MODULE.find_latest_release("owner/repo")

    def test_current_sha_rejects_absolute_current_symlink(self) -> None:
        release_root = self.root / "release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root)
        self.run_quietly(
            MODULE.install_release_tree, release_root, home, SHA1, dry_run=False
        )
        current = home / "personal-sync" / "current"
        current.unlink()
        current.symlink_to(
            home / "personal-sync" / "releases" / SHA1, target_is_directory=True
        )

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
                github_release_asset(
                    101,
                    archive_name,
                    size=archive_path.stat().st_size,
                    digest=github_sha256(archive_path.read_bytes()),
                ),
                github_release_asset(
                    102,
                    checksum_name,
                    size=checksum_path.stat().st_size,
                    digest=github_sha256(checksum_path.read_bytes()),
                ),
            ],
        }

        def fake_download(repo, assets, destination, *, workspace):
            self.assertEqual(repo, "owner/repo")
            self.assertEqual(assets.archive_name, archive_name)
            shutil.copy2(archive_path, destination / archive_name)
            shutil.copy2(checksum_path, destination / checksum_name)

        with (
            mock.patch.object(MODULE, "find_latest_release", return_value=release),
            mock.patch.object(MODULE, "download_release_assets", fake_download),
        ):
            self.run_quietly(
                MODULE.install_from_github, "owner/repo", home, dry_run=False
            )

        self.assertEqual(current_target(home), f"releases/{SHA1}")
        self.assertTrue((home / "AGENTS.md").is_symlink())

    def test_install_from_github_rejects_replaced_extracted_release_root(
        self,
    ) -> None:
        release_root = self.root / "downloaded-release"
        retained_release = self.root / "retained-downloaded-release"
        home = self.root / "home" / ".codex"
        write_minimal_release(release_root, agent_text="verified\n")
        release_expectation = MODULE._source_release_identity(release_root, None)
        release_root.rename(retained_release)
        write_minimal_release(release_root, agent_text="substitute\n")
        assets = MODULE.ReleaseAssets(
            tag_name="personal-codex-20260511-120000-1111111",
            sha=SHA1,
            archive_name=f"personal-codex-{SHA1}.tar.gz",
            checksum_name=f"personal-codex-{SHA1}.sha256",
            archive_id=101,
            archive_size=1,
            checksum_id=102,
            checksum_size=1,
        )

        def fake_download(repo, destination, *, workspace, sha=None):
            self.assertEqual(repo, "owner/repo")
            self.assertIsNone(sha)
            return MODULE.DownloadedRelease(
                repo=repo,
                assets=assets,
                release_root=release_root,
                release_expectation=release_expectation,
            )

        with (
            mock.patch.object(
                MODULE,
                "download_and_extract_release",
                side_effect=fake_download,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "release source changed after its captured identity",
            ),
        ):
            self.run_quietly(
                MODULE.install_from_github,
                "owner/repo",
                home,
                dry_run=False,
            )

        self.assertFalse((home / "personal-sync" / "current").exists())

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
                github_release_asset(
                    101,
                    archive_name,
                    size=archive_path.stat().st_size,
                    digest=github_sha256(archive_path.read_bytes()),
                ),
                github_release_asset(
                    102,
                    checksum_name,
                    size=checksum_path.stat().st_size,
                    digest=github_sha256(checksum_path.read_bytes()),
                ),
            ],
        }

        def fake_download(repo, assets, destination, *, workspace):
            shutil.copy2(archive_path, destination / archive_name)
            shutil.copy2(checksum_path, destination / checksum_name)

        with (
            mock.patch.object(MODULE, "find_latest_release", return_value=release),
            mock.patch.object(MODULE, "download_release_assets", fake_download),
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "checksum mismatch"):
                self.run_quietly(
                    MODULE.install_from_github, "owner/repo", home, dry_run=False
                )

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
                "run-scheduled",
                "--mode",
                "public",
                "--repo",
                "owner/repo",
                "--home",
                str(home),
            ],
        )
        self.assertEqual(
            payload["EnvironmentVariables"],
            {
                "HOME": str(self.user_home),
                "PATH": MODULE.MACOS_SCHEDULER_PATH,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        self.assertEqual(payload["LimitLoadToSessionType"], "Background")
        self.assertEqual(payload["ProcessType"], "Background")
        self.assertIs(payload["LowPriorityIO"], True)
        self.assertEqual(payload["ThrottleInterval"], 60)
        self.assertEqual(payload["Umask"], 0o077)
        self.assertEqual(payload["WorkingDirectory"], str(self.user_home))
        self.assertIn("codex-personal-sync.out.log", payload["StandardOutPath"])

    def test_bare_install_scheduler_migrates_legacy_gui_plist_to_background(
        self,
    ) -> None:
        home = self.user_home / ".codex"
        runner = write_scheduler_runner(home)
        paths = MODULE._scheduler_paths("macos", home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        legacy = MODULE._launchd_plist(
            home,
            "owner/repo",
            71,
            runner,
        )
        for key in (
            "LimitLoadToSessionType",
            "LowPriorityIO",
            "ProcessType",
            "ThrottleInterval",
            "Umask",
            "WorkingDirectory",
        ):
            legacy.pop(key, None)
        legacy["EnvironmentVariables"].pop("HOME", None)
        paths.launchd_plist.write_bytes(plistlib.dumps(legacy, sort_keys=True))

        self.run_quietly(
            MODULE.install_scheduler,
            home,
            None,
            None,
            "macos",
            None,
            dry_run=False,
            enable=False,
        )

        with paths.launchd_plist.open("rb") as file:
            migrated = plistlib.load(file)
        self.assertEqual(migrated["LimitLoadToSessionType"], "Background")
        self.assertEqual(migrated["ProcessType"], "Background")
        self.assertEqual(migrated["WorkingDirectory"], str(self.user_home))
        self.assertEqual(
            migrated["EnvironmentVariables"]["HOME"],
            str(self.user_home),
        )
        self.assertEqual(migrated["StartInterval"], 71 * 60)

    def test_bare_install_scheduler_preserves_legacy_private_target(self) -> None:
        home = self.user_home / ".codex"
        runner = write_scheduler_runner(home)
        paths = MODULE._scheduler_paths("macos", home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        legacy = MODULE._launchd_plist(
            home,
            "Joey-Tools/codex-private-workflows",
            73,
            runner,
            mode="private",
            base_repo="Joey-Tools/codex-toolbox",
            owner="private",
        )
        legacy["ProgramArguments"] = [
            str(runner),
            "install-private",
            "--repo",
            "Joey-Tools/codex-private-workflows",
            "--base-repo",
            "Joey-Tools/codex-toolbox",
            "--owner",
            "private",
            "--home",
            str(home),
        ]
        paths.launchd_plist.write_bytes(plistlib.dumps(legacy, sort_keys=True))
        before = MODULE._load_macos_scheduler_config(paths)
        assert before is not None
        self.assertEqual(before.command, "install-private")

        self.run_quietly(
            MODULE.install_scheduler,
            home,
            None,
            None,
            "macos",
            None,
            dry_run=False,
            enable=False,
        )

        migrated = MODULE._load_macos_scheduler_config(paths)
        assert migrated is not None
        self.assertEqual(migrated.command, "run-scheduled")
        self.assertEqual(migrated.mode, "private")
        self.assertEqual(migrated.repo, "Joey-Tools/codex-private-workflows")
        self.assertEqual(migrated.base_repo, "Joey-Tools/codex-toolbox")
        self.assertEqual(migrated.owner, "private")
        self.assertEqual(migrated.interval_minutes, 73)

    def test_ordinary_release_install_does_not_mutate_scheduler(self) -> None:
        home = self.user_home / ".codex"
        initial_release_root = self.root / "initial-release"
        write_minimal_release(initial_release_root)
        (initial_release_root / "scripts" / "codex_personal_sync.py").chmod(0o755)
        self.run_quietly(
            MODULE.install_release_tree,
            initial_release_root,
            home,
            "2" * 40,
            dry_run=False,
        )
        self.run_quietly(
            MODULE.install_scheduler,
            home,
            "owner/public-sync",
            61,
            "macos",
            None,
            dry_run=False,
            enable=False,
        )
        paths = MODULE._scheduler_paths("macos", home)
        assert paths.launchd_plist is not None
        before_metadata = paths.launchd_plist.stat()
        before = paths.launchd_plist.read_bytes()
        release_root = self.root / "ordinary-release"
        write_minimal_release(release_root)

        self.run_quietly(
            MODULE.install_release_tree,
            release_root,
            home,
            "3" * 40,
            dry_run=False,
        )

        after_metadata = paths.launchd_plist.stat()
        self.assertEqual(paths.launchd_plist.read_bytes(), before)
        self.assertEqual(
            (after_metadata.st_dev, after_metadata.st_ino),
            (before_metadata.st_dev, before_metadata.st_ino),
        )

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
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: ["/bin/launchctl", *args[1:]],
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                return_value=completed,
            ) as run,
        ):
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
        uid = os.getuid()
        gui_target = f"gui/{uid}/{MODULE.LAUNCHD_LABEL}"
        user_domain = f"user/{uid}"
        user_target = f"{user_domain}/{MODULE.LAUNCHD_LABEL}"
        current_identity_calls = [
            ["/bin/launchctl", "bootout", gui_target],
            ["/bin/launchctl", "disable", gui_target],
            ["/bin/launchctl", "bootout", user_target],
            ["/bin/launchctl", "enable", user_target],
            ["/bin/launchctl", "bootstrap", user_domain, str(plist_path)],
            ["/bin/launchctl", "enable", user_target],
        ]
        self.assertEqual(
            [call for call in calls if call in current_identity_calls],
            current_identity_calls,
        )
        self.assertFalse(
            any(call[:2] == ["/bin/launchctl", "kickstart"] for call in calls)
        )

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
        service = (unit_root / "codex-personal-sync.service").read_text(
            encoding="utf-8"
        )
        timer = (unit_root / "codex-personal-sync.timer").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn(f'Environment="PATH={MODULE.LINUX_SCHEDULER_PATH}"', service)
        self.assertIn('Environment="PYTHONDONTWRITEBYTECODE=1"', service)
        self.assertIn(f'ExecStart="{runner}" "run-scheduled"', service)
        self.assertIn('"--mode" "public"', service)
        self.assertIn('"--repo" "owner/repo"', service)
        self.assertIn(f'"--home" "{home}"', service)
        self.assertIn("OnBootSec=5min", timer)
        self.assertIn("OnUnitActiveSec=45min", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_install_scheduler_runs_linux_enable_commands(self) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: ["/usr/bin/systemctl", *args[1:]],
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                return_value=completed,
            ) as run,
        ):
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
        self.assertIn(["/usr/bin/systemctl", "--user", "daemon-reload"], calls)
        self.assertIn(
            [
                "/usr/bin/systemctl",
                "--user",
                "start",
                "codex-personal-sync.timer",
            ],
            calls,
        )
        enablement = (
            self.root
            / "home"
            / ".config"
            / "systemd"
            / "user"
            / "timers.target.wants"
            / "codex-personal-sync.timer"
        )
        self.assertEqual(
            os.readlink(enablement), str(enablement.parent.parent / enablement.name)
        )

    def test_install_scheduler_rejects_drop_in_when_linux_units_are_absent(
        self,
    ) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        unit_root = self.root / "home" / ".config" / "systemd" / "user"
        drop_in = unit_root / "codex-personal-sync.service.d"
        drop_in.mkdir(parents=True)
        override = drop_in / "override.conf"
        override.write_text(
            "[Service]\nExecStartPre=/tmp/attacker\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "drop-ins are unsupported",
        ):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

        self.assertFalse((unit_root / "codex-personal-sync.service").exists())
        self.assertFalse((unit_root / "codex-personal-sync.timer").exists())
        self.assertEqual(
            override.read_text(encoding="utf-8"),
            "[Service]\nExecStartPre=/tmp/attacker\n",
        )

    def test_linux_enable_rejects_drop_in_appearing_after_daemon_reload(
        self,
    ) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        unit_root = self.root / "home" / ".config" / "systemd" / "user"
        calls: list[list[str]] = []

        def inject_after_reload(
            args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            calls.append(args)
            if args == ["systemctl", "--user", "daemon-reload"]:
                drop_in = unit_root / "codex-personal-sync.service.d"
                drop_in.mkdir()
                (drop_in / "override.conf").write_text(
                    "[Service]\nExecStartPre=/tmp/attacker\n",
                    encoding="utf-8",
                )

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=inject_after_reload,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "drop-ins are unsupported",
            ),
        ):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            calls,
            [["systemctl", "--user", "daemon-reload"]],
        )

    def test_linux_enable_rejects_main_unit_drift_during_daemon_reload(
        self,
    ) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        paths = MODULE._scheduler_paths("linux", home)
        assert paths.systemd_service is not None
        calls: list[list[str]] = []

        def mutate_during_reload(
            args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            calls.append(args)
            if args == ["systemctl", "--user", "daemon-reload"]:
                paths.systemd_service.write_text(
                    "[Unit]\nDescription=foreign replacement\n",
                    encoding="utf-8",
                )
                paths.systemd_service.chmod(0o600)

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=mutate_during_reload,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "(?:command-interval name/content stability|"
                "Linux systemd scheduler service content changed "
                "after native action systemctl --user daemon-reload)",
            ),
        ):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            calls,
            [["systemctl", "--user", "daemon-reload"]],
        )

    def test_linux_enable_rejects_main_unit_drift_before_start(self) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        paths = MODULE._scheduler_paths("linux", home)
        assert paths.systemd_service is not None
        calls: list[list[str]] = []

        real_enablement = MODULE._ensure_systemd_timer_enablement

        def replace_after_enablement(
            selected_paths: MODULE.SchedulerPaths,
            *,
            dry_run: bool,
        ) -> MODULE.SymlinkSnapshot | None:
            result = real_enablement(selected_paths, dry_run=dry_run)
            paths.systemd_service.unlink()
            paths.systemd_service.write_text(
                "[Unit]\nDescription=foreign replacement\n",
                encoding="utf-8",
            )
            paths.systemd_service.chmod(0o600)
            return result

        def capture_native(
            args: list[str],
            **_kwargs,
        ) -> None:
            calls.append(args)

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=capture_native,
            ),
            mock.patch.object(
                MODULE,
                "_ensure_systemd_timer_enablement",
                side_effect=replace_after_enablement,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "published systemd scheduler service/timer pair changed",
            ),
        ):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            calls,
            [["systemctl", "--user", "daemon-reload"]],
        )

    def test_linux_enable_rejects_enablement_drift_after_start(self) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        paths = MODULE._scheduler_paths("linux", home)
        enablement = MODULE._systemd_timer_enablement_path(paths)
        calls: list[list[str]] = []

        def replace_enablement_after_start(
            args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            calls.append(args)
            if args == [
                "systemctl",
                "--user",
                "start",
                f"{MODULE.SYSTEMD_UNIT}.timer",
            ]:
                enablement.unlink()
                enablement.symlink_to("foreign.timer")

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=replace_enablement_after_start,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "timer enablement changed after publication",
            ),
        ):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            calls,
            [
                ["systemctl", "--user", "daemon-reload"],
                [
                    "systemctl",
                    "--user",
                    "start",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
            ],
        )
        self.assertEqual(os.readlink(enablement), "foreign.timer")
        self.assertTrue(MODULE._scheduler_activation_transaction_path(paths).exists())

    def test_linux_enable_keeps_exact_published_unit_snapshot(
        self,
    ) -> None:
        for drift in ("same-content-replacement", "mode"):
            with self.subTest(drift=drift):
                case_user_home = self.root / f"home-{drift}"
                home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    write_scheduler_runner(home)
                    paths = MODULE._scheduler_paths("linux", home)
                    assert paths.systemd_service is not None
                    marker_path = MODULE._scheduler_pair_transaction_path(paths)
                    real_remove = MODULE._remove_scheduler_config_if_snapshot
                    native_calls: list[list[str]] = []
                    injected = False

                    def drift_before_pair_commit(
                        path: Path,
                        expected: MODULE.ManagedStateFileSnapshot,
                    ) -> None:
                        nonlocal injected
                        if path == marker_path and not injected:
                            injected = True
                            if drift == "same-content-replacement":
                                payload = paths.systemd_service.read_bytes()
                                paths.systemd_service.unlink()
                                paths.systemd_service.write_bytes(payload)
                                paths.systemd_service.chmod(0o600)
                            else:
                                paths.systemd_service.chmod(0o640)
                        real_remove(path, expected)

                    with (
                        mock.patch.object(
                            MODULE,
                            "_remove_scheduler_config_if_snapshot",
                            side_effect=drift_before_pair_commit,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=lambda args, **_kwargs: native_calls.append(
                                args
                            ),
                        ),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "published systemd scheduler service/timer pair changed",
                        ),
                    ):
                        MODULE.install_scheduler(
                            home,
                            "owner/repo",
                            60,
                            "linux",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertTrue(injected)
                    self.assertEqual(native_calls, [])

    def test_systemd_pair_revalidation_detects_interleaved_service_drift(
        self,
    ) -> None:
        for drift in ("same-content-replacement", "mode"):
            with self.subTest(drift=drift):
                case_user_home = self.root / f"pair-read-{drift}"
                home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    write_scheduler_runner(home)
                    self.run_quietly(
                        MODULE.install_scheduler,
                        home,
                        "owner/repo",
                        60,
                        "linux",
                        None,
                        dry_run=False,
                        enable=False,
                    )
                    paths = MODULE._scheduler_paths("linux", home)
                    assert paths.systemd_service is not None
                    assert paths.systemd_timer is not None
                    expected = (
                        MODULE._scheduler_config_snapshot(paths.systemd_service),
                        MODULE._scheduler_config_snapshot(paths.systemd_timer),
                    )
                    real_open = MODULE.os.open
                    injected = False

                    def drift_before_timer_open(
                        path: str | bytes,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        nonlocal injected
                        if (
                            path == paths.systemd_timer.name
                            and dir_fd is not None
                            and not injected
                        ):
                            injected = True
                            if drift == "same-content-replacement":
                                payload = paths.systemd_service.read_bytes()
                                paths.systemd_service.unlink()
                                paths.systemd_service.write_bytes(payload)
                                paths.systemd_service.chmod(0o600)
                            else:
                                paths.systemd_service.chmod(0o640)
                        return real_open(
                            path,
                            flags,
                            mode,
                            dir_fd=dir_fd,
                        )

                    with (
                        MODULE._retain_launchd_activation_binding(
                            paths.systemd_service,
                            expected[0],
                            description="Linux systemd scheduler service",
                            revalidate_on_exit=False,
                        ) as service_binding,
                        MODULE._retain_launchd_activation_binding(
                            paths.systemd_timer,
                            expected[1],
                            description="Linux systemd scheduler timer",
                            revalidate_on_exit=False,
                        ) as timer_binding,
                        mock.patch.object(
                            MODULE.os,
                            "open",
                            side_effect=drift_before_timer_open,
                        ),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "published systemd scheduler service/timer pair changed",
                        ),
                    ):
                        MODULE._revalidate_published_systemd_pair(
                            paths,
                            expected,
                            (service_binding, timer_binding),
                        )

                    self.assertTrue(injected)

    def test_systemd_publication_binding_rejects_simulated_inode_reuse(
        self,
    ) -> None:
        user_home = self.root / "publication-reuse"
        unit = user_home / ".config" / "systemd" / "user" / "unit.service"
        unit.parent.mkdir(parents=True)
        with mock.patch.object(MODULE.Path, "home", return_value=user_home):
            before = MODULE._scheduler_config_snapshot(unit)
            installed, binding = MODULE._write_text_with_activation_binding(
                unit,
                "stable payload\n",
                expected_snapshot=before,
                description="Linux systemd scheduler service",
            )
            payload = unit.read_bytes()
            unit.unlink()
            unit.write_bytes(payload)
            unit.chmod(0o600)
            real_stat = MODULE.os.stat

            def report_reused_identity(path, *args, **kwargs):
                observed = real_stat(path, *args, **kwargs)
                if path == unit.name and kwargs.get("dir_fd") == binding.parent_fd:
                    fields = list(observed)
                    fields[1] = installed.file_identity[1]
                    fields[2] = installed.file_identity[0]
                    return os.stat_result(fields)
                return observed

            try:
                with (
                    mock.patch.object(
                        MODULE.os,
                        "stat",
                        side_effect=report_reused_identity,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "object identity changed",
                    ),
                ):
                    MODULE._revalidate_launchd_activation_binding(
                        binding,
                        boundary="before daemon reload",
                    )
            finally:
                MODULE._release_retained_scheduler_activation_binding(
                    binding,
                    revalidate=False,
                )

    def test_systemd_publication_binding_allows_benign_metadata_churn(
        self,
    ) -> None:
        user_home = self.root / "publication-benign"
        unit = user_home / ".config" / "systemd" / "user" / "unit.service"
        alias = unit.with_name("unit-alias.service")
        unit.parent.mkdir(parents=True)
        with mock.patch.object(MODULE.Path, "home", return_value=user_home):
            before = MODULE._scheduler_config_snapshot(unit)
            _installed, binding = MODULE._write_text_with_activation_binding(
                unit,
                "stable payload\n",
                expected_snapshot=before,
                description="Linux systemd scheduler service",
            )
            os.utime(unit, None)
            os.link(unit, alias)
            try:
                MODULE._revalidate_launchd_activation_binding(
                    binding,
                    boundary="before daemon reload",
                )
            finally:
                alias.unlink()
                MODULE._release_retained_scheduler_activation_binding(
                    binding,
                    revalidate=True,
                )

    def test_linux_activation_rejects_temporary_replace_consume_restore(
        self,
    ) -> None:
        for config_matches in (False, True):
            with self.subTest(config_matches=config_matches):
                case_user_home = self.root / f"activation-aba-{config_matches}"
                home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    write_scheduler_runner(home)
                    paths = MODULE._scheduler_paths("linux", home)
                    assert paths.systemd_service is not None
                    if config_matches:
                        self.run_quietly(
                            MODULE.install_scheduler,
                            home,
                            "owner/repo",
                            60,
                            "linux",
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    mutation_generation = 0
                    observed_generation = 0
                    native_calls: list[list[str]] = []
                    consumed_payloads: list[bytes] = []
                    watch_token = object()

                    @contextlib.contextmanager
                    def retain_guard(_paths, _bindings, _drop_ins):
                        yield watch_token

                    def reset_generation(
                        guard,
                        *,
                        boundary: str,
                    ) -> None:
                        del boundary
                        self.assertIs(guard, watch_token)

                    def revalidate_guard(
                        guard,
                        *,
                        boundary: str,
                        compare_generation: bool,
                    ) -> bool:
                        nonlocal observed_generation
                        del boundary
                        self.assertIs(guard, watch_token)
                        if (
                            compare_generation
                            and mutation_generation != observed_generation
                        ):
                            observed_generation = mutation_generation
                            return False
                        return True

                    def replace_consume_restore(
                        args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        nonlocal mutation_generation
                        del dry_run, allow_fail
                        native_calls.append(args)
                        if args != ["systemctl", "--user", "daemon-reload"]:
                            return
                        if consumed_payloads:
                            return
                        backup = paths.systemd_service.with_name(
                            f"{paths.systemd_service.name}.retained"
                        )
                        paths.systemd_service.rename(backup)
                        paths.systemd_service.write_text(
                            "[Unit]\nDescription=foreign replacement\n",
                            encoding="utf-8",
                        )
                        paths.systemd_service.chmod(0o600)
                        consumed_payloads.append(paths.systemd_service.read_bytes())
                        paths.systemd_service.unlink()
                        backup.rename(paths.systemd_service)
                        mutation_generation += 1

                    with (
                        mock.patch.object(
                            MODULE,
                            "_retain_systemd_activation_stability_guard",
                            side_effect=retain_guard,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_reset_systemd_activation_generation",
                            side_effect=reset_generation,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_revalidate_systemd_activation_stability_guard",
                            side_effect=revalidate_guard,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=replace_consume_restore,
                        ),
                    ):
                        MODULE.install_scheduler(
                            home,
                            "owner/repo",
                            60,
                            "linux",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertEqual(
                        consumed_payloads,
                        [b"[Unit]\nDescription=foreign replacement\n"],
                    )
                    self.assertEqual(
                        native_calls,
                        [
                            ["systemctl", "--user", "daemon-reload"],
                            ["systemctl", "--user", "daemon-reload"],
                            [
                                "systemctl",
                                "--user",
                                "start",
                                f"{MODULE.SYSTEMD_UNIT}.timer",
                            ],
                        ],
                    )
                    self.assertTrue(
                        paths.systemd_service.read_text().startswith("[Unit]")
                    )
                    self.assertNotIn(
                        "foreign replacement",
                        paths.systemd_service.read_text(),
                    )
                    self.assertFalse(
                        MODULE._scheduler_activation_transaction_path(paths).exists()
                    )

    def test_linux_activation_stops_after_bounded_unstable_reloads(
        self,
    ) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        paths = MODULE._scheduler_paths("linux", home)
        guard_token = object()
        native_calls: list[list[str]] = []

        @contextlib.contextmanager
        def retain_guard(_paths, _bindings, _drop_ins):
            yield guard_token

        with (
            mock.patch.object(
                MODULE,
                "_retain_systemd_activation_stability_guard",
                side_effect=retain_guard,
            ),
            mock.patch.object(
                MODULE,
                "_reset_systemd_activation_generation",
            ),
            mock.patch.object(
                MODULE,
                "_revalidate_systemd_activation_stability_guard",
                return_value=False,
            ),
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=lambda args, **_kwargs: native_calls.append(args),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "could not produce a stable daemon-reload after 3 attempts",
            ),
        ):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            native_calls,
            [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "daemon-reload"],
            ],
        )
        self.assertTrue(MODULE._scheduler_activation_transaction_path(paths).exists())

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "Linux file leases are required",
    )
    def test_linux_activation_guard_observes_replace_restore(self) -> None:
        user_home = self.root / "activation-watch"
        service = (
            user_home
            / ".config"
            / "systemd"
            / "user"
            / f"{MODULE.SYSTEMD_UNIT}.service"
        )
        timer = service.with_name(f"{MODULE.SYSTEMD_UNIT}.timer")
        service.parent.mkdir(parents=True)
        bindings: list[MODULE.SchedulerActivationBinding] = []
        with mock.patch.object(MODULE.Path, "home", return_value=user_home):
            try:
                for path, payload, description in (
                    (service, "service\n", "Linux systemd scheduler service"),
                    (timer, "timer\n", "Linux systemd scheduler timer"),
                ):
                    before = MODULE._scheduler_config_snapshot(path)
                    _installed, binding = MODULE._write_text_with_activation_binding(
                        path,
                        payload,
                        expected_snapshot=before,
                        description=description,
                    )
                    bindings.append(binding)
                paths = MODULE._scheduler_paths(
                    "linux",
                    user_home / ".codex",
                )
                drop_ins = MODULE._audit_systemd_drop_ins(paths)
                with MODULE._retain_systemd_activation_stability_guard(
                    paths,
                    tuple(bindings),
                    drop_ins,
                ) as guard:
                    assert guard is not None
                    MODULE._reset_systemd_activation_generation(
                        guard,
                        boundary="before simulated daemon reload",
                    )
                    backup = service.with_name(f"{service.name}.retained")
                    service.rename(backup)
                    service.write_text("foreign\n", encoding="utf-8")
                    service.chmod(0o600)
                    self.assertEqual(service.read_text(), "foreign\n")
                    service.unlink()
                    backup.rename(service)
                    self.assertFalse(
                        MODULE._revalidate_systemd_activation_stability_guard(
                            guard,
                            boundary="after simulated daemon reload",
                            compare_generation=True,
                        )
                    )
            finally:
                for binding in bindings:
                    MODULE._release_retained_scheduler_activation_binding(
                        binding,
                        revalidate=True,
                    )

    def test_scheduler_atomic_write_closes_stream_fd_when_fdopen_fails(
        self,
    ) -> None:
        user_home = self.root / "fdopen-failure"
        user_home.mkdir()
        with mock.patch.object(MODULE.Path, "home", return_value=user_home):
            for retain_description in (
                None,
                "Linux systemd scheduler service",
            ):
                with self.subTest(retain=retain_description is not None):
                    path = (
                        user_home
                        / ".config"
                        / "systemd"
                        / "user"
                        / (
                            "retained.service"
                            if retain_description is not None
                            else "ordinary.service"
                        )
                    )
                    captured_fds: list[int] = []

                    def fail_fdopen(file_fd, *_args, **_kwargs):
                        captured_fds.append(file_fd)
                        raise OSError("injected fdopen failure")

                    with (
                        mock.patch.object(
                            MODULE.os,
                            "fdopen",
                            side_effect=fail_fdopen,
                        ),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "injected fdopen failure",
                        ),
                    ):
                        MODULE._atomic_write_scheduler_config(
                            path,
                            b"payload\n",
                            retain_activation_description=retain_description,
                        )

                    self.assertEqual(len(captured_fds), 1)
                    with self.assertRaises(OSError):
                        os.fstat(captured_fds[0])
                    self.assertFalse(path.exists())
                    self.assertFalse(
                        any(
                            "personal-sync-write" in candidate.name
                            for candidate in path.parent.iterdir()
                        )
                    )

    def test_linux_uninstall_preserves_foreign_drop_in_and_blocks_reinstall(
        self,
    ) -> None:
        home = self.root / "home" / ".codex"
        write_scheduler_runner(home)
        self.run_quietly(
            MODULE.install_scheduler,
            home,
            "owner/repo",
            60,
            "linux",
            None,
            dry_run=False,
            enable=False,
        )
        unit_root = self.root / "home" / ".config" / "systemd" / "user"
        drop_in = unit_root / "codex-personal-sync.timer.d"
        drop_in.mkdir()
        override = drop_in / "override.conf"
        override.write_text(
            "[Timer]\nOnUnitActiveSec=1min\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            MODULE.uninstall_scheduler(
                home,
                "linux",
                dry_run=False,
                disable=False,
            )

        self.assertIn(
            "preserved foreign systemd drop-in residue",
            output.getvalue(),
        )
        self.assertTrue(override.is_file())
        self.assertFalse((unit_root / "codex-personal-sync.service").exists())
        self.assertFalse((unit_root / "codex-personal-sync.timer").exists())
        report = MODULE.scheduler_report(home, "linux")
        self.assertIn("drop-ins are unsupported", report.failure_reason or "")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "drop-ins are unsupported",
        ):
            MODULE.install_scheduler(
                home,
                "owner/repo",
                60,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

        override.unlink()
        drop_in.rmdir()
        self.run_quietly(
            MODULE.install_scheduler,
            home,
            "owner/repo",
            60,
            "linux",
            None,
            dry_run=False,
            enable=False,
        )
        self.assertTrue((unit_root / "codex-personal-sync.service").is_file())
        self.assertTrue((unit_root / "codex-personal-sync.timer").is_file())

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

    def test_launchctl_quoted_not_loaded_requires_exact_label_and_uid(
        self,
    ) -> None:
        uid = str(os.getuid())
        for label in (MODULE.LAUNCHD_LABEL, *MODULE.LEGACY_LAUNCHD_LABELS):
            for domain_kind, evidence_domain in (
                ("gui", f"user gui: {uid}"),
                ("user", f"uid: {uid}"),
            ):
                args = [
                    "launchctl",
                    "bootout",
                    f"{domain_kind}/{uid}/{label}",
                ]
                other_evidence_domain = (
                    f"uid: {uid}" if domain_kind == "gui" else f"user gui: {uid}"
                )
                for description, evidence, accepted in (
                    (
                        "exact",
                        f'Could not find service "{label}" '
                        f"in domain for {evidence_domain}",
                        True,
                    ),
                    (
                        "cross-domain",
                        f'Could not find service "{label}" '
                        f"in domain for {other_evidence_domain}",
                        False,
                    ),
                    (
                        "label case drift",
                        f'Could not find service "{label.upper()}" '
                        f"in domain for {evidence_domain}",
                        False,
                    ),
                    (
                        "wrong uid",
                        f'Could not find service "{label}" '
                        f"in domain for "
                        + (
                            f"user gui: {int(uid) + 1}"
                            if domain_kind == "gui"
                            else f"uid: {int(uid) + 1}"
                        ),
                        False,
                    ),
                ):
                    with self.subTest(
                        label=label,
                        domain=domain_kind,
                        description=description,
                    ):
                        completed = subprocess.CompletedProcess(
                            args,
                            113,
                            "",
                            f"Bad request.\n{evidence}\n",
                        )
                        self.assertEqual(
                            MODULE._native_scheduler_failure_is_already_absent(
                                args,
                                completed,
                            ),
                            accepted,
                        )

    def test_uninstall_scheduler_runs_macos_disable_commands(self) -> None:
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
        completed = subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: ["/bin/launchctl", *args[1:]],
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                return_value=completed,
            ) as run,
        ):
            self.run_quietly(
                MODULE.uninstall_scheduler,
                home,
                "macos",
                dry_run=False,
                disable=True,
            )

        uid = os.getuid()
        calls = [call.args[0] for call in run.call_args_list]
        for domain in (f"user/{uid}", f"gui/{uid}"):
            target = f"{domain}/{MODULE.LAUNCHD_LABEL}"
            self.assertIn(["/bin/launchctl", "bootout", target], calls)
            self.assertIn(["/bin/launchctl", "disable", target], calls)


if __name__ == "__main__":
    unittest.main()
