from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_scheduler_doctor_tests",
    SCRIPT_PATH,
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PUBLIC_SHA = "1" * 40
PRIVATE_SHA = "2" * 40


def snapshot_tree(root: Path) -> tuple[tuple[str, str, int, bytes | str | None], ...]:
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


class SchedulerDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="scheduler-doctor.")
        self.root = Path(os.path.realpath(self.tmpdir.name))
        self.user_home = self.root / "home"
        self.home = self.user_home / ".codex"
        self.home.mkdir(parents=True)
        self.path_home_patch = mock.patch.object(
            MODULE.Path,
            "home",
            return_value=self.user_home,
        )
        self.path_home_patch.start()
        self.host_mirror_private_control_parent = MODULE.MIRROR_PRIVATE_CONTROL_PARENT
        self.host_mirror_private_control_root_specs = (
            MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS
        )
        self.mirror_private_control_parent = (
            self.root / MODULE.MIRROR_PRIVATE_CONTROL_NAMESPACE_NAME
        )
        self.mirror_private_control_parent.mkdir(mode=0o700)
        self.mirror_private_control_parent_patch = mock.patch.object(
            MODULE,
            "MIRROR_PRIVATE_CONTROL_PARENT",
            self.mirror_private_control_parent,
        )
        self.mirror_private_control_parent_patch.start()
        self.mirror_private_control_root_specs_patch = mock.patch.object(
            MODULE,
            "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
            (
                MODULE.MirrorPrivateControlRootSpec(
                    root_id="test-primary-home-v1",
                    parent_path=self.mirror_private_control_parent,
                    allocate=True,
                    account_home=self.root,
                    shared_parent=False,
                ),
            ),
        )
        self.mirror_private_control_root_specs_patch.start()

    def tearDown(self) -> None:
        self.mirror_private_control_root_specs_patch.stop()
        self.mirror_private_control_parent_patch.stop()
        self.path_home_patch.stop()
        self.tmpdir.cleanup()

    def write_runner(self) -> Path:
        runner = self.home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        return runner

    def install_scheduler_quietly(
        self,
        repo: str,
        interval_minutes: int | None,
        platform_name: str,
        *,
        enable: bool = False,
        mode: str = "public",
        base_repo: str = MODULE.DEFAULT_PUBLIC_RELEASE_REPO,
        owner: str = "private",
    ) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.install_scheduler(
                self.home,
                repo,
                interval_minutes,
                platform_name,
                None,
                dry_run=False,
                enable=enable,
                mode=mode,
                base_repo=base_repo,
                owner=owner,
            )

    def write_pending_systemd_pair(
        self,
        user_home: Path,
        home: Path,
    ) -> MODULE.SchedulerPaths:
        runner = home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        with mock.patch.object(MODULE.Path, "home", return_value=user_home):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.install_scheduler(
                    home,
                    "owner/old",
                    17,
                    "linux",
                    None,
                    dry_run=False,
                    enable=False,
                )
            paths = MODULE._scheduler_paths("linux", home)
            assert paths.systemd_service is not None
            assert paths.systemd_timer is not None
            service_before = MODULE._scheduler_config_snapshot(paths.systemd_service)
            timer_before = MODULE._scheduler_config_snapshot(paths.systemd_timer)
            service_after = MODULE._systemd_service(
                home,
                "owner/new",
                runner,
            ).encode("utf-8")
            timer_after = MODULE._systemd_timer(29).encode("utf-8")
            marker = MODULE._scheduler_pair_transaction_path(paths)
            marker_before = MODULE._scheduler_config_snapshot(
                marker,
                MODULE.MAX_SCHEDULER_PAIR_TRANSACTION_BYTES,
            )
            MODULE._atomic_write_scheduler_config(
                marker,
                MODULE._scheduler_pair_transaction_payload(
                    service_before=service_before,
                    timer_before=timer_before,
                    service_after=service_after,
                    timer_after=timer_after,
                ),
                expected_snapshot=marker_before,
            )
            MODULE._atomic_write_scheduler_config(
                paths.systemd_service,
                service_after,
                expected_snapshot=service_before,
            )
            MODULE._atomic_write_scheduler_config(
                paths.systemd_timer,
                timer_after,
                expected_snapshot=timer_before,
            )
        return paths

    def write_skill(self, name: str, frontmatter_name: str) -> Path:
        skill_root = self.home / "skills" / name
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name}\n---\n",
            encoding="utf-8",
        )
        return skill_root

    def test_macos_scheduler_config_parses_private_run_scheduled(self) -> None:
        runner = self.home / "bin" / "runner with spaces"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        paths.launchd_plist.write_bytes(
            plistlib.dumps(
                MODULE._launchd_plist(
                    self.home,
                    "owner/private-sync",
                    23,
                    runner,
                    mode="private",
                    base_repo="owner/public-sync",
                    owner="private",
                ),
                sort_keys=True,
            )
        )

        config = MODULE._load_macos_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.platform, "macos")
        self.assertEqual(config.config_paths, (paths.launchd_plist,))
        self.assertEqual(config.interval_minutes, 23)
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.command, "run-scheduled")
        self.assertEqual(config.mode, "private")
        self.assertEqual(config.repo, "owner/private-sync")
        self.assertEqual(config.base_repo, "owner/public-sync")
        self.assertEqual(config.owner, "private")

    def test_linux_scheduler_config_parses_private_run_scheduled(self) -> None:
        runner = self.home / "bin" / "runner with spaces"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/private-sync",
                runner,
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(47),
            encoding="utf-8",
        )

        config = MODULE._load_linux_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.platform, "linux")
        self.assertEqual(
            config.config_paths,
            (paths.systemd_service, paths.systemd_timer),
        )
        self.assertEqual(config.interval_minutes, 47)
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.command, "run-scheduled")
        self.assertEqual(config.mode, "private")
        self.assertEqual(config.repo, "owner/private-sync")
        self.assertEqual(config.base_repo, "owner/public-sync")
        self.assertEqual(config.owner, "private")

    def test_scheduler_config_read_tolerates_mtime_only_churn(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(37),
            encoding="utf-8",
        )
        real_read = MODULE.os.read
        touched = False

        def read_then_touch(file_fd: int, size: int) -> bytes:
            nonlocal touched
            payload = real_read(file_fd, size)
            if not payload and not touched:
                touched = True
                metadata = paths.systemd_service.stat()
                os.utime(
                    paths.systemd_service,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000_000,
                    ),
                )
            return payload

        with mock.patch.object(MODULE.os, "read", side_effect=read_then_touch):
            config = MODULE._load_linux_scheduler_config(paths)

        self.assertTrue(touched)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.interval_minutes, 37)

    def test_scheduler_config_read_rejects_same_inode_byte_drift(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        original = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        )
        paths.systemd_service.write_text(original, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(37),
            encoding="utf-8",
        )
        real_read = MODULE.os.read
        mutated = False

        def read_then_mutate(file_fd: int, size: int) -> bytes:
            nonlocal mutated
            payload = real_read(file_fd, size)
            if not payload and not mutated:
                mutated = True
                paths.systemd_service.write_text(
                    original.replace("Type=oneshot", "Type=onefail"),
                    encoding="utf-8",
                )
            return payload

        with (
            mock.patch.object(MODULE.os, "read", side_effect=read_then_mutate),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "content changed during read",
            ),
        ):
            MODULE._load_linux_scheduler_config(paths)

        self.assertTrue(mutated)

    def test_scheduler_install_args_use_stable_run_scheduled_entrypoint(self) -> None:
        runner = Path("/opt/codex/bin/codex-personal-sync")

        public_args = MODULE._scheduler_install_args(
            runner,
            "owner/public-sync",
            self.home,
        )
        private_args = MODULE._scheduler_install_args(
            runner,
            "owner/private-sync",
            self.home,
            mode="private",
            base_repo="owner/public-sync",
            owner="private",
        )

        self.assertEqual(
            public_args,
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "public",
                "--repo",
                "owner/public-sync",
                "--home",
                str(self.home),
            ],
        )
        self.assertEqual(
            private_args,
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "private",
                "--repo",
                "owner/private-sync",
                "--base-repo",
                "owner/public-sync",
                "--owner",
                "private",
                "--home",
                str(self.home),
            ],
        )

    def test_install_preserves_audited_existing_interval_when_omitted(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        legacy_payload = MODULE._launchd_plist(
            self.home,
            "owner/old-sync",
            17,
            runner,
        )
        legacy_payload["ProgramArguments"] = [
            str(runner),
            "install",
            "--repo",
            "owner/old-sync",
            "--home",
            str(self.home),
        ]
        paths.launchd_plist.write_bytes(plistlib.dumps(legacy_payload, sort_keys=True))

        self.install_scheduler_quietly(
            "owner/new-sync",
            None,
            "macos",
        )

        with paths.launchd_plist.open("rb") as stream:
            installed = plistlib.load(stream)
        self.assertEqual(installed["StartInterval"], 17 * 60)
        self.assertEqual(
            installed["ProgramArguments"],
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "public",
                "--repo",
                "owner/new-sync",
                "--home",
                str(self.home),
            ],
        )

    def test_exact_audited_config_avoids_reinstall_and_rewrite(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            29,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        before = (
            paths.systemd_service.read_bytes(),
            paths.systemd_timer.read_bytes(),
            paths.systemd_service.stat().st_mtime_ns,
            paths.systemd_timer.stat().st_mtime_ns,
        )

        output = io.StringIO()
        with (
            mock.patch.object(MODULE, "_write_text") as write_text,
            mock.patch.object(MODULE, "_run_native_command") as run_native,
            contextlib.redirect_stdout(output),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                None,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        write_text.assert_not_called()
        self.assertEqual(
            run_native.call_args_list,
            [
                mock.call(
                    ["systemctl", "--user", "daemon-reload"],
                    dry_run=False,
                    allow_fail=False,
                ),
                mock.call(
                    [
                        "systemctl",
                        "--user",
                        "start",
                        f"{MODULE.SYSTEMD_UNIT}.timer",
                    ],
                    dry_run=False,
                    allow_fail=False,
                ),
            ],
        )
        enablement = MODULE._systemd_timer_enablement_path(paths)
        self.assertEqual(os.readlink(enablement), str(paths.systemd_timer))
        self.assertIn("already matches audited configuration", output.getvalue())
        self.assertIn("preserved 29-minute interval", output.getvalue())
        self.assertEqual(
            (
                paths.systemd_service.read_bytes(),
                paths.systemd_timer.read_bytes(),
                paths.systemd_service.stat().st_mtime_ns,
                paths.systemd_timer.stat().st_mtime_ns,
            ),
            before,
        )

    def test_active_scheduler_change_without_enable_requires_reactivation(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                with mock.patch.object(MODULE, "_run_native_command"):
                    self.install_scheduler_quietly(
                        f"owner/{platform_name}-sync",
                        17,
                        platform_name,
                        enable=True,
                    )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                marker = MODULE._scheduler_activation_transaction_path(paths)
                self.assertFalse(marker.exists())

                self.install_scheduler_quietly(
                    f"owner/{platform_name}-sync",
                    29,
                    platform_name,
                    enable=False,
                )
                self.assertTrue(marker.exists())
                config_paths = tuple(
                    path
                    for path in (
                        paths.launchd_plist,
                        paths.systemd_service,
                        paths.systemd_timer,
                    )
                    if path is not None
                )
                before = tuple(
                    (
                        path.read_bytes(),
                        path.stat().st_dev,
                        path.stat().st_ino,
                        path.stat().st_mtime_ns,
                    )
                    for path in config_paths
                )

                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ) as daemon_enabled:
                    report = MODULE.scheduler_report(self.home, platform_name)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _doctor_report, issues = MODULE.doctor(
                            self.home,
                            platform_name,
                            json_output=False,
                        )
                daemon_enabled.assert_not_called()
                self.assertTrue(report.installed)
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertIsNotNone(report.daemon_query)
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )
                self.assertNotIn(
                    "scheduler-daemon-unavailable",
                    {code for code, _reason in report.failures},
                )
                self.assertTrue(
                    any(
                        issue.code == "scheduler-activation-incomplete"
                        and issue.path == marker
                        for issue in issues
                    )
                )

                writer_name = (
                    "_write_plist" if platform_name == "macos" else "_write_text"
                )
                with (
                    mock.patch.object(MODULE, writer_name) as writer,
                    mock.patch.object(MODULE, "_run_native_command"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-sync",
                        None,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                writer.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual(
                    tuple(
                        (
                            path.read_bytes(),
                            path.stat().st_dev,
                            path.stat().st_ino,
                            path.stat().st_mtime_ns,
                        )
                        for path in config_paths
                    ),
                    before,
                )
                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ):
                    recovered = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertTrue(recovered.enabled)
                self.assertNotEqual(
                    recovered.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertEqual(recovered.interval_minutes, 29)

    def test_activation_failure_persists_marker_until_exact_retry(self) -> None:
        self.write_runner()
        failure_actions = {
            "macos": "bootstrap",
            "linux": "daemon-reload",
        }
        for platform_name, failure_action in failure_actions.items():
            with self.subTest(platform=platform_name):
                paths = MODULE._scheduler_paths(platform_name, self.home)
                marker = MODULE._scheduler_activation_transaction_path(paths)

                def fail_activation(
                    args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool | str = False,
                ) -> None:
                    del dry_run, allow_fail
                    if failure_action in args:
                        raise MODULE.SyncError(f"simulated {failure_action} failure")

                with (
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=fail_activation,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        f"simulated {failure_action} failure",
                    ),
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-failure",
                        31,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                self.assertTrue(marker.exists())
                config_paths = tuple(
                    path
                    for path in (
                        paths.launchd_plist,
                        paths.systemd_service,
                        paths.systemd_timer,
                    )
                    if path is not None
                )
                before = tuple(
                    (
                        path.read_bytes(),
                        path.stat().st_dev,
                        path.stat().st_ino,
                        path.stat().st_mtime_ns,
                    )
                    for path in config_paths
                )
                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ) as daemon_enabled:
                    report = MODULE.scheduler_report(self.home, platform_name)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _doctor_report, issues = MODULE.doctor(
                            self.home,
                            platform_name,
                            json_output=False,
                        )
                daemon_enabled.assert_not_called()
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertTrue(
                    any(
                        issue.code == "scheduler-activation-incomplete"
                        and issue.path == marker
                        for issue in issues
                    )
                )

                writer_name = (
                    "_write_plist" if platform_name == "macos" else "_write_text"
                )
                with (
                    mock.patch.object(MODULE, writer_name) as writer,
                    mock.patch.object(MODULE, "_run_native_command"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-failure",
                        None,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                writer.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual(
                    tuple(
                        (
                            path.read_bytes(),
                            path.stat().st_dev,
                            path.stat().st_ino,
                            path.stat().st_mtime_ns,
                        )
                        for path in config_paths
                    ),
                    before,
                )
                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ):
                    recovered = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertTrue(recovered.enabled)
                self.assertEqual(recovered.interval_minutes, 31)

    def test_invalid_activation_state_fails_closed_and_uninstall_clears_it(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    f"owner/{platform_name}-invalid",
                    23,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                marker = MODULE._scheduler_activation_transaction_path(paths)
                marker.write_text("{invalid\n", encoding="utf-8")

                with mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=MODULE.SchedulerDaemonQuery("enabled"),
                ) as daemon_enabled:
                    report = MODULE.scheduler_report(self.home, platform_name)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _doctor_report, issues = MODULE.doctor(
                            self.home,
                            platform_name,
                            json_output=False,
                        )
                daemon_enabled.assert_not_called()
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-state-invalid",
                )
                self.assertTrue(
                    any(
                        issue.code == "scheduler-activation-state-invalid"
                        and issue.path == marker
                        for issue in issues
                    )
                )

                writer_name = (
                    "_write_plist" if platform_name == "macos" else "_write_text"
                )
                with (
                    mock.patch.object(MODULE, writer_name) as writer,
                    mock.patch.object(MODULE, "_run_native_command") as native,
                    self.assertRaises(MODULE.SyncError) as raised,
                ):
                    MODULE.install_scheduler(
                        self.home,
                        f"owner/{platform_name}-invalid",
                        37,
                        platform_name,
                        None,
                        dry_run=False,
                        enable=True,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "scheduler-activation-state-invalid",
                )
                writer.assert_not_called()
                native.assert_not_called()

                with contextlib.redirect_stdout(io.StringIO()):
                    MODULE.uninstall_scheduler(
                        self.home,
                        platform_name,
                        dry_run=False,
                        disable=False,
                    )
                self.assertFalse(marker.exists())

    def test_status_report_includes_config_releases_and_runtime_health(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/private-sync",
                runner,
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(31),
            encoding="utf-8",
        )
        runtime_state = {
            "version": 1,
            "last_attempt": "2026-07-23T09:15:00+00:00",
            "last_success": "2026-07-23T08:15:00+00:00",
            "success": False,
            "failure_reason": "network unavailable",
            "mode": "private",
            "repo": "owner/private-sync",
            "base_repo": "owner/public-sync",
            "owner": "private",
        }
        output = io.StringIO()

        with (
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(
                    (MODULE.PUBLIC_OWNER, PUBLIC_SHA),
                    ("private", PRIVATE_SHA),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            contextlib.redirect_stdout(output),
        ):
            report = MODULE.status_scheduler(
                self.home,
                "linux",
                json_output=True,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(report.platform, "linux")
        self.assertTrue(report.installed)
        self.assertEqual(
            payload,
            {
                "platform": "linux",
                "installed": True,
                "enabled": True,
                "config": [
                    str(paths.systemd_service),
                    str(paths.systemd_timer),
                ],
                "interval_minutes": 31,
                "runner": str(runner),
                "stable_runner": False,
                "command": "run-scheduled",
                "mode": "private",
                "repo": "owner/private-sync",
                "base_repo": "owner/public-sync",
                "private_repo": "owner/private-sync",
                "owner": "private",
                "migration_needed": False,
                "last_attempt": "2026-07-23T09:15:00+00:00",
                "recent_success": "2026-07-23T08:15:00+00:00",
                "current_release": {
                    MODULE.PUBLIC_OWNER: PUBLIC_SHA,
                    "private": PRIVATE_SHA,
                },
                "release_integrity": [],
                "quarantine_batches": 0,
                "quarantine_limit": MODULE.MAX_RETAINED_QUARANTINE_BATCHES,
                "mirror_quarantine": MODULE._mirror_quarantine_payload(
                    report.mirror_quarantine
                ),
                "failure_code": None,
                "failure_reason": "network unavailable",
                "daemon_query": {
                    "classification": "enabled",
                    "reason": None,
                },
                "failures": [
                    {
                        "code": None,
                        "reason": "network unavailable",
                    },
                    {
                        "code": "scheduler-runner-drift",
                        "reason": (
                            "scheduler does not use the stable installed runner path"
                        ),
                    },
                ],
            },
        )

    def test_status_reports_reconstructable_legacy_private_migration(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        service_lines = MODULE._systemd_service(
            self.home,
            "owner/private-sync",
            runner,
            mode="private",
            base_repo="owner/public-sync",
            owner="private",
        ).splitlines()
        legacy_arguments = [
            str(runner),
            "install-private",
            "--repo",
            "owner/private-sync",
            "--base-repo",
            "owner/public-sync",
            "--owner",
            "private",
            "--home",
            str(self.home),
        ]
        legacy_exec = " ".join(
            MODULE._systemd_quote(argument) for argument in legacy_arguments
        )
        service_lines = [
            f"ExecStart={legacy_exec}" if line.startswith("ExecStart=") else line
            for line in service_lines
        ]
        paths.systemd_service.write_text(
            "\n".join(service_lines),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(MODULE._systemd_timer(47), encoding="utf-8")

        with (
            mock.patch.object(
                MODULE, "_read_scheduler_runtime_state", return_value=None
            ),
            mock.patch.object(
                MODULE, "_current_releases_for_scheduler", return_value=()
            ),
            mock.patch.object(MODULE, "_scheduler_daemon_enabled", return_value=False),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        payload = MODULE._scheduler_report_payload(report)
        self.assertEqual(payload["command"], "install-private")
        self.assertEqual(payload["mode"], "private")
        self.assertEqual(payload["repo"], "owner/private-sync")
        self.assertEqual(payload["base_repo"], "owner/public-sync")
        self.assertEqual(payload["owner"], "private")
        self.assertEqual(payload["interval_minutes"], 47)
        self.assertTrue(payload["migration_needed"])

    def test_run_scheduled_persists_success_state(self) -> None:
        with (
            mock.patch.object(MODULE, "install_from_github") as install,
            mock.patch.object(
                MODULE,
                "_capture_scheduler_release_trees",
                return_value={},
            ),
            mock.patch.object(
                MODULE,
                "_write_scheduler_runtime_state",
                wraps=MODULE._write_scheduler_runtime_state,
            ) as write_state,
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/public-sync",
                mode="public",
                base_repo="owner/ignored-base",
                owner="private",
            )

        install.assert_called_once_with(
            "owner/public-sync",
            self.home,
            dry_run=False,
        )
        self.assertEqual(write_state.call_count, 2)
        initial = write_state.call_args_list[0].args[1]
        final = write_state.call_args_list[1].args[1]
        self.assertFalse(initial["success"])
        self.assertEqual(
            initial["failure_reason"],
            "scheduled sync did not complete",
        )
        self.assertTrue(final["success"])
        self.assertIsNone(final["failure_reason"])
        self.assertEqual(final["last_attempt"], final["last_success"])
        self.assertEqual(final["mode"], "public")
        self.assertEqual(final["repo"], "owner/public-sync")
        self.assertEqual(final["base_repo"], "owner/public-sync")
        self.assertEqual(final["owner"], MODULE.PUBLIC_OWNER)
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            final,
        )
        self.assertEqual(
            stat.S_IMODE(MODULE._scheduler_status_path(self.home).stat().st_mode),
            0o600,
        )

    def test_run_scheduled_persists_failure_and_previous_success(self) -> None:
        previous_success = "2026-07-22T08:00:00+00:00"
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 1,
                "last_attempt": previous_success,
                "last_success": previous_success,
                "success": True,
                "failure_reason": None,
                "mode": "private",
                "repo": "owner/private-sync",
                "base_repo": "owner/public-sync",
                "owner": "private",
            },
        )

        with (
            mock.patch.object(
                MODULE,
                "install_private_from_github",
                side_effect=MODULE.SyncError("network unavailable"),
            ) as install,
            mock.patch.object(
                MODULE,
                "_write_scheduler_runtime_state",
                wraps=MODULE._write_scheduler_runtime_state,
            ) as write_state,
            self.assertRaisesRegex(MODULE.SyncError, "network unavailable"),
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/private-sync",
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            )

        install.assert_called_once_with(
            "owner/private-sync",
            self.home,
            base_repo="owner/public-sync",
            owner="private",
            dry_run=False,
        )
        self.assertEqual(write_state.call_count, 2)
        initial = write_state.call_args_list[0].args[1]
        final = write_state.call_args_list[1].args[1]
        self.assertFalse(initial["success"])
        self.assertEqual(initial["last_success"], previous_success)
        self.assertFalse(final["success"])
        self.assertEqual(final["last_success"], previous_success)
        self.assertEqual(final["failure_reason"], "network unavailable")
        self.assertEqual(final["mode"], "private")
        self.assertEqual(final["repo"], "owner/private-sync")
        self.assertEqual(final["base_repo"], "owner/public-sync")
        self.assertEqual(final["owner"], "private")
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            final,
        )

    def test_overlapping_scheduled_completion_cannot_replace_newer_failure(
        self,
    ) -> None:
        older_install_entered = threading.Event()
        release_older_install = threading.Event()
        errors: dict[str, BaseException] = {}

        def interleaved_install(
            repo: str,
            home: Path,
            *,
            dry_run: bool,
        ) -> None:
            self.assertEqual(repo, "owner/public-sync")
            self.assertEqual(home, self.home)
            self.assertFalse(dry_run)
            if threading.current_thread().name == "older-scheduled-run":
                older_install_entered.set()
                if not release_older_install.wait(5):
                    raise AssertionError("older scheduled run was not released")
                return
            raise MODULE.SyncError(
                "newer scheduled run failed",
                code="newer-attempt-failed",
            )

        def run(name: str) -> None:
            try:
                MODULE.run_scheduled(
                    self.home,
                    "owner/public-sync",
                    mode="public",
                    base_repo="owner/ignored",
                    owner="private",
                )
            except BaseException as error:
                errors[name] = error

        older = threading.Thread(
            target=run,
            args=("older",),
            name="older-scheduled-run",
            daemon=True,
        )
        newer = threading.Thread(
            target=run,
            args=("newer",),
            name="newer-scheduled-run",
            daemon=True,
        )
        with (
            mock.patch.object(
                MODULE,
                "install_from_github",
                side_effect=interleaved_install,
            ),
            mock.patch.object(
                MODULE,
                "_capture_scheduler_release_trees",
                return_value={},
            ),
        ):
            try:
                older.start()
                self.assertTrue(older_install_entered.wait(5))
                older_incomplete = MODULE._read_scheduler_runtime_state(self.home)
                assert older_incomplete is not None

                newer.start()
                newer.join(5)
                self.assertFalse(newer.is_alive())
                newer_failure = MODULE._read_scheduler_runtime_state(self.home)
                assert newer_failure is not None
                self.assertIsInstance(errors.get("newer"), MODULE.SyncError)
                self.assertFalse(newer_failure["success"])
                self.assertEqual(
                    newer_failure["failure_code"],
                    "newer-attempt-failed",
                )
                self.assertGreater(
                    datetime.fromisoformat(newer_failure["last_attempt"]),
                    datetime.fromisoformat(older_incomplete["last_attempt"]),
                )
            finally:
                release_older_install.set()
                if older.ident is not None:
                    older.join(5)
                if newer.ident is not None:
                    newer.join(5)
            self.assertFalse(older.is_alive())

        self.assertNotIn("older", errors)
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            newer_failure,
        )

    def test_superseded_scheduled_install_cannot_roll_back_newer_release(
        self,
    ) -> None:
        older_sha = "3" * 40
        newer_sha = "4" * 40
        older_release = self.root / "older-release"
        newer_release = self.root / "newer-release"

        def write_release(root: Path, payload: str) -> None:
            skill_root = root / "personal_codex" / "skills" / "scheduler-race"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(payload, encoding="utf-8")
            (root / "personal_codex" / "sync-manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "source": "personal_codex/skills/scheduler-race",
                                "target": "skills/scheduler-race",
                                "kind": "skill",
                            }
                        ],
                        "reference_only": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        write_release(older_release, "---\nname: scheduler-race\nold: true\n---\n")
        write_release(newer_release, "---\nname: scheduler-race\nnew: true\n---\n")
        older_download_entered = threading.Event()
        release_older_download = threading.Event()
        errors: dict[str, BaseException] = {}

        def downloaded_release(root: Path, sha: str) -> MODULE.DownloadedRelease:
            return MODULE.DownloadedRelease(
                repo="owner/public-sync",
                assets=MODULE.ReleaseAssets(
                    tag_name=f"personal-codex-20260723-120000-{sha[:7]}",
                    sha=sha,
                    archive_name=f"personal-codex-{sha}.tar.gz",
                    archive_id=1,
                    archive_size=1,
                    checksum_name=f"personal-codex-{sha}.sha256",
                    checksum_id=2,
                    checksum_size=1,
                ),
                release_root=root,
            )

        def interleaved_download(
            repo: str,
            destination: Path,
            *,
            workspace,
            sha: str | None = None,
        ) -> MODULE.DownloadedRelease:
            del destination, workspace, sha
            self.assertEqual(repo, "owner/public-sync")
            if threading.current_thread().name == "older-release-run":
                older_download_entered.set()
                if not release_older_download.wait(5):
                    raise AssertionError("older release download was not resumed")
                return downloaded_release(older_release, older_sha)
            return downloaded_release(newer_release, newer_sha)

        def run(name: str) -> None:
            try:
                MODULE.run_scheduled(
                    self.home,
                    "owner/public-sync",
                    mode="public",
                    base_repo="owner/ignored",
                    owner="private",
                )
            except BaseException as error:
                errors[name] = error

        older = threading.Thread(
            target=run,
            args=("older",),
            name="older-release-run",
            daemon=True,
        )
        newer = threading.Thread(
            target=run,
            args=("newer",),
            name="newer-release-run",
            daemon=True,
        )
        with mock.patch.object(
            MODULE,
            "download_and_extract_release",
            side_effect=interleaved_download,
        ):
            try:
                older.start()
                self.assertTrue(older_download_entered.wait(5))
                newer.start()
                newer.join(10)
                self.assertFalse(newer.is_alive())
                self.assertNotIn("newer", errors)
                self.assertEqual(
                    MODULE._current_sha(self.home, MODULE.PUBLIC_OWNER),
                    newer_sha,
                )
                newer_state = MODULE._read_scheduler_runtime_state(self.home)
                assert newer_state is not None
                self.assertTrue(newer_state["success"])
                self.assertEqual(
                    newer_state["release_trees"][MODULE.PUBLIC_OWNER]["sha"],
                    newer_sha,
                )
            finally:
                release_older_download.set()
                if older.ident is not None:
                    older.join(10)
                if newer.ident is not None:
                    newer.join(10)

        self.assertFalse(older.is_alive())
        self.assertIsInstance(errors.get("older"), MODULE.SyncError)
        assert isinstance(errors["older"], MODULE.SyncError)
        self.assertEqual(errors["older"].code, "scheduled-sync-superseded")
        self.assertEqual(
            MODULE._current_sha(self.home, MODULE.PUBLIC_OWNER),
            newer_sha,
        )
        self.assertFalse(
            (self.home / "personal-sync" / "releases" / older_sha).exists()
        )
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            newer_state,
        )

    def test_scheduler_attempt_recovers_from_unbounded_or_noncanonical_time(
        self,
    ) -> None:
        invalid_attempts = (
            "9999-12-31T23:59:59.999999+00:00",
            "9999-12-31T23:59:59.999999-23:59",
            "2026-07-23T09:15:00Z",
            "2026-07-23T09:15:00",
            "not-a-timestamp",
        )

        for previous_attempt in invalid_attempts:
            with self.subTest(previous_attempt=previous_attempt):
                attempt = MODULE._next_scheduler_attempt(
                    {"last_attempt": previous_attempt}
                )
                parsed = datetime.fromisoformat(attempt)
                self.assertEqual(parsed.utcoffset(), timedelta(0))
                self.assertEqual(parsed.isoformat(), attempt)
                self.assertNotEqual(attempt, previous_attempt)
                self.assertLess(parsed.year, 9999)

    def test_scheduler_attempt_rejects_far_future_but_preserves_cas_order(
        self,
    ) -> None:
        far_future = (
            datetime.now(timezone.utc)
            + MODULE.MAX_SCHEDULER_ATTEMPT_FUTURE_SKEW
            + timedelta(days=1)
        ).isoformat()
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 2,
                "last_attempt": far_future,
                "last_success": far_future,
                "success": True,
                "failure_reason": "injected future state",
                "failure_code": "injected-future-state",
                "release_trees": {
                    MODULE.PUBLIC_OWNER: {
                        "sha": PUBLIC_SHA,
                        "tree_sha256": "a" * 64,
                    }
                },
                "mode": "public",
                "repo": "owner/public-sync",
                "base_repo": "owner/public-sync",
                "owner": MODULE.PUBLIC_OWNER,
            },
        )
        with self.assertRaises(MODULE.SyncError) as invalid_state:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            invalid_state.exception.code,
            "scheduler-state-timestamp-invalid",
        )

        replacement_attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertNotEqual(replacement_attempt, far_future)
        self.assertFalse(
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )
        )
        state = MODULE._read_scheduler_runtime_state(self.home)
        assert state is not None
        self.assertEqual(state["last_attempt"], replacement_attempt)
        self.assertIsNone(state["last_success"])
        self.assertEqual(state["release_trees"], {})

    def test_scheduler_runtime_cas_preserves_identity_replacement(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        replacement = b"foreign newer scheduler state\n"
        real_publish = MODULE._atomic_write_scheduler_config

        def replace_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.unlink()
            status_path.write_bytes(replacement)
            status_path.chmod(0o600)
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=replace_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), replacement)

    def test_scheduler_runtime_cas_does_not_restore_unproven_late_replacement(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        newer = b"foreign state that won the late race\n"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0
        reader_blocked = False

        def replace_live_then_exchange(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count, reader_blocked
            exchange_count += 1
            if exchange_count == 1:
                try:
                    MODULE._read_scheduler_runtime_state(self.home)
                except MODULE.SyncError as error:
                    reader_blocked = (
                        error.code == "scheduler-state-publication-incomplete"
                    )
                else:
                    self.fail(
                        "runtime reader accepted state while publication marker "
                        "was active"
                    )
                marker_path.unlink()
                status_path.unlink()
                status_path.write_bytes(newer)
                status_path.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=replace_live_then_exchange,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "could not prove that the temporary object is the exact state "
                "displaced",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(exchange_count, 1)
        self.assertTrue(reader_blocked)
        self.assertNotEqual(status_path.read_bytes(), newer)
        self.assertEqual(
            json.loads(status_path.read_text(encoding="utf-8"))["last_attempt"],
            attempt,
        )
        displaced_paths = [
            candidate
            for candidate in status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*"
            )
            if not candidate.name.endswith(".original")
        ]
        self.assertEqual(len(displaced_paths), 1)
        self.assertEqual(displaced_paths[0].read_bytes(), newer)
        self.assertTrue(marker_path.is_file())
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unresolved publication marker",
        ) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        report = MODULE.scheduler_report(self.home, "linux")
        self.assertEqual(
            report.failure_code,
            "scheduler-state-publication-incomplete",
        )
        self.assertIsNone(report.last_attempt)
        self.assertIsNone(report.recent_success)

    def test_scheduler_runtime_residue_blocks_when_marker_rebuild_fails(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        newer = b"foreign state that won the late race\n"
        real_create = MODULE._create_scheduler_runtime_publication_marker
        real_exchange = MODULE._rename_exchange_at
        create_calls = 0

        def create_once_then_fail(
            user_home: Path,
            path: Path,
            parent_fd: int,
            payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal create_calls
            create_calls += 1
            if create_calls == 1:
                return real_create(
                    user_home,
                    path,
                    parent_fd,
                    payload,
                )
            raise MODULE.SyncError("injected marker recreation failure")

        def delete_marker_and_replace_live(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            marker_path.unlink()
            status_path.unlink()
            status_path.write_bytes(newer)
            status_path.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_create_scheduler_runtime_publication_marker",
                side_effect=create_once_then_fail,
            ),
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=delete_marker_and_replace_live,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "marker recreation failure",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(create_calls, 3)
        self.assertFalse(marker_path.exists())
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_cas_never_swaps_unproven_temp_back_to_live(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original = status_path.read_bytes()
        attacker = b"attacker-controlled temporary replacement\n"
        saved_displaced = status_path.parent / "saved-displaced-status"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0

        def swap_displaced_temp(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count
            exchange_count += 1
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )
            if exchange_count != 1:
                return
            saved_displaced.write_bytes(attacker)
            saved_displaced.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                first_parent_fd,
                saved_displaced.name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=swap_displaced_temp,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "could not prove that the temporary object is the exact state "
                "displaced",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(exchange_count, 1)
        self.assertNotEqual(status_path.read_bytes(), attacker)
        self.assertEqual(
            json.loads(status_path.read_text(encoding="utf-8"))["last_attempt"],
            attempt,
        )
        recovery_paths = list(
            status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*.original"
            )
        )
        self.assertEqual(len(recovery_paths), 1)
        self.assertEqual(recovery_paths[0].read_bytes(), original)
        displaced_paths = [
            candidate
            for candidate in status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*"
            )
            if not candidate.name.endswith(".original")
        ]
        self.assertEqual(len(displaced_paths), 1)
        self.assertEqual(displaced_paths[0].read_bytes(), attacker)
        self.assertEqual(saved_displaced.read_bytes(), original)
        self.assertTrue(
            MODULE._scheduler_runtime_publication_marker_path(status_path).is_file()
        )

    def test_scheduler_runtime_reader_rejects_marker_appearing_after_read(
        self,
    ) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        real_check = MODULE._scheduler_runtime_publication_is_incomplete
        checks = 0

        def appear_on_post_check(
            home: Path,
            checked_status_path: Path,
            parent_fd: int,
        ) -> bool:
            nonlocal checks
            checks += 1
            if checks == 2:
                marker_path.write_text(
                    '{"version":1,"status":"incomplete"}\n',
                    encoding="utf-8",
                )
                marker_path.chmod(0o600)
            return real_check(home, checked_status_path, parent_fd)

        with (
            mock.patch.object(
                MODULE,
                "_scheduler_runtime_publication_is_incomplete",
                side_effect=appear_on_post_check,
            ),
            self.assertRaises(MODULE.SyncError) as error_context,
        ):
            MODULE._read_scheduler_runtime_state(self.home)

        self.assertEqual(checks, 2)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_reader_directly_stats_fixed_marker(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        marker_path.write_text(
            '{"version":1,"status":"incomplete"}\n',
            encoding="utf-8",
        )
        marker_path.chmod(0o600)
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with mock.patch.object(
                MODULE,
                "_directory_member_names",
                return_value=(status_path.name,),
            ):
                self.assertTrue(
                    MODULE._scheduler_runtime_publication_is_incomplete(
                        self.home,
                        status_path,
                        parent_fd,
                    )
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_casefold_aliases(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        transaction_prefix = f".{status_path.name}.personal-sync-write-"
        retained_marker_prefix = (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{marker_path.name}-"
        )
        retained_transaction_prefix = (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{transaction_prefix}"
        )
        aliases = (
            marker_path.name.swapcase(),
            f"{transaction_prefix.swapcase()}case-alias",
            f"{retained_marker_prefix.swapcase()}case-alias",
            f"{retained_transaction_prefix.swapcase()}case-alias",
        )
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            for alias in aliases:
                with (
                    self.subTest(alias=alias),
                    mock.patch.object(
                        MODULE,
                        "_directory_member_names",
                        return_value=(status_path.name, alias),
                    ),
                ):
                    self.assertTrue(
                        MODULE._scheduler_runtime_publication_is_incomplete(
                            self.home,
                            status_path,
                            parent_fd,
                        )
                    )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_nfd_residue_alias(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        canonical_status_path = MODULE._scheduler_status_path(self.home)
        status_path = canonical_status_path.with_name("scheduler-Café.json")
        nfd_residue = ".scheduler-Cafe\u0301.json.personal-sync-write-nfd-alias"
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with mock.patch.object(
                MODULE,
                "_directory_member_names",
                return_value=(canonical_status_path.name, nfd_residue),
            ):
                self.assertTrue(
                    MODULE._scheduler_runtime_publication_is_incomplete(
                        self.home,
                        status_path,
                        parent_fd,
                    )
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_normalized_alias_collision(
        self,
    ) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        canonical_status_path = MODULE._scheduler_status_path(self.home)
        status_path = canonical_status_path.with_name("scheduler-Café.json")
        nfc_residue = ".scheduler-Café.json.personal-sync-write-normalized-alias"
        nfd_residue = ".scheduler-Cafe\u0301.json.personal-sync-write-normalized-alias"
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_directory_member_names",
                    return_value=(
                        canonical_status_path.name,
                        nfc_residue,
                        nfd_residue,
                    ),
                ),
                self.assertRaises(MODULE.SyncError) as error_context,
            ):
                MODULE._scheduler_runtime_publication_is_incomplete(
                    self.home,
                    status_path,
                    parent_fd,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        self.assertIn(
            "ambiguous portable aliases",
            str(error_context.exception),
        )

    def test_scheduler_runtime_writer_refuses_preexisting_marker(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original = status_path.read_bytes()
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        marker_path.write_text(
            '{"version":1,"status":"incomplete"}\n',
            encoding="utf-8",
        )
        marker_path.chmod(0o600)

        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        self.assertEqual(status_path.read_bytes(), original)

    def test_scheduler_runtime_marker_cleanup_failure_remains_fail_closed(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        real_commit = MODULE._commit_scheduler_runtime_publication_marker
        injected = False
        reader_blocked = False

        def replace_marker_before_commit(
            user_home: Path,
            path: Path,
            parent_fd: int,
            expected: MODULE.ManagedStateFileSnapshot,
        ) -> None:
            nonlocal injected, reader_blocked
            if not injected:
                injected = True
                try:
                    MODULE._read_scheduler_runtime_state(self.home)
                except MODULE.SyncError as error:
                    reader_blocked = (
                        error.code == "scheduler-state-publication-incomplete"
                    )
                else:
                    self.fail(
                        "runtime reader accepted state before the marker commit "
                        "linearization point"
                    )
                marker_path.unlink()
                marker_path.write_bytes(b"foreign marker replacement\n")
                marker_path.chmod(0o600)
            real_commit(
                user_home,
                path,
                parent_fd,
                expected,
            )

        with (
            mock.patch.object(
                MODULE,
                "_commit_scheduler_runtime_publication_marker",
                side_effect=replace_marker_before_commit,
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertTrue(injected)
        self.assertTrue(reader_blocked)
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        self.assertTrue(marker_path.exists())
        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_cas_rejects_same_inode_content_and_access_drift(
        self,
    ) -> None:
        for drift in ("content", "access"):
            with self.subTest(drift=drift):
                case_home = self.home / f"runtime-{drift}"
                case_home.mkdir()
                attempt = MODULE._begin_scheduler_attempt(
                    case_home,
                    mode="public",
                    repo="owner/public-sync",
                    base_repo="owner/public-sync",
                    owner=MODULE.PUBLIC_OWNER,
                )
                status_path = MODULE._scheduler_status_path(case_home)
                original_identity = (
                    status_path.stat().st_dev,
                    status_path.stat().st_ino,
                )
                real_publish = MODULE._atomic_write_scheduler_config

                def drift_before_publish(
                    path: Path,
                    payload: bytes,
                    *,
                    expected_snapshot: (MODULE.ManagedStateFileSnapshot | None) = None,
                    mode: int = 0o600,
                    gid: int | None = None,
                    rollback_displaced_conflict: bool = False,
                ) -> None:
                    if drift == "content":
                        with status_path.open("r+b") as stream:
                            stream.seek(0)
                            stream.write(b"foreign same-inode state\n")
                            stream.truncate()
                            stream.flush()
                            os.fsync(stream.fileno())
                    else:
                        status_path.chmod(0o640)
                    real_publish(
                        path,
                        payload,
                        expected_snapshot=expected_snapshot,
                        mode=mode,
                        gid=gid,
                        rollback_displaced_conflict=rollback_displaced_conflict,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_atomic_write_scheduler_config",
                        side_effect=drift_before_publish,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "changed before conditional publication",
                    ),
                ):
                    MODULE._complete_scheduler_attempt(
                        case_home,
                        attempt=attempt,
                        success=True,
                        failure_reason=None,
                        failure_code=None,
                        mode="public",
                        repo="owner/public-sync",
                        base_repo="owner/public-sync",
                        owner=MODULE.PUBLIC_OWNER,
                    )

                self.assertEqual(
                    (
                        status_path.stat().st_dev,
                        status_path.stat().st_ino,
                    ),
                    original_identity,
                )
                if drift == "content":
                    self.assertEqual(
                        status_path.read_bytes(),
                        b"foreign same-inode state\n",
                    )
                else:
                    self.assertEqual(
                        stat.S_IMODE(status_path.stat().st_mode),
                        0o640,
                    )

    def test_scheduler_runtime_cas_preserves_absent_to_appeared_state(
        self,
    ) -> None:
        status_path = MODULE._scheduler_status_path(self.home)
        appeared = b"foreign concurrently-created scheduler state\n"
        real_publish = MODULE._atomic_write_scheduler_config

        def appear_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.write_bytes(appeared)
            status_path.chmod(0o600)
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=appear_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._begin_scheduler_attempt(
                self.home,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), appeared)

    def test_scheduler_runtime_cas_allows_mtime_only_transition(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        real_publish = MODULE._atomic_write_scheduler_config

        def touch_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            metadata = status_path.stat()
            os.utime(
                status_path,
                ns=(
                    metadata.st_atime_ns,
                    metadata.st_mtime_ns + 1_000_000,
                ),
            )
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_write_scheduler_config",
            side_effect=touch_before_publish,
        ):
            completed = MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertTrue(completed)
        state = MODULE._read_scheduler_runtime_state(self.home)
        assert state is not None
        self.assertTrue(state["success"])
        self.assertEqual(state["last_attempt"], attempt)
        self.assertFalse(
            MODULE._scheduler_runtime_publication_marker_path(status_path).exists()
        )

    def test_scheduler_runtime_cas_rejects_parent_rotation_with_same_file_inode(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original_payload = status_path.read_bytes()
        original_identity = (
            status_path.stat().st_dev,
            status_path.stat().st_ino,
        )
        rotated_parent = status_path.parent.with_name(
            status_path.parent.name + "-rotated"
        )
        real_publish = MODULE._atomic_write_scheduler_config

        def rotate_parent_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.parent.rename(rotated_parent)
            status_path.parent.mkdir(mode=0o700)
            os.link(
                rotated_parent / status_path.name,
                status_path,
                follow_symlinks=False,
            )
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=rotate_parent_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), original_payload)
        self.assertEqual(
            (status_path.stat().st_dev, status_path.stat().st_ino),
            original_identity,
        )
        self.assertEqual(
            (
                (rotated_parent / status_path.name).stat().st_dev,
                (rotated_parent / status_path.name).stat().st_ino,
            ),
            original_identity,
        )
        self.assertEqual(
            list(status_path.parent.glob(f".{status_path.name}.personal-sync-write-*")),
            [],
        )

    def test_timestamp_recovery_does_not_accept_unsafe_runtime_file(self) -> None:
        status_path = MODULE._scheduler_status_path(self.home)
        status_path.parent.mkdir(parents=True)
        status_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "last_attempt": "not-a-timestamp",
                    "last_success": None,
                    "success": False,
                    "failure_reason": None,
                    "failure_code": None,
                    "release_trees": {},
                    "mode": "public",
                    "repo": "owner/public-sync",
                    "base_repo": "owner/public-sync",
                    "owner": MODULE.PUBLIC_OWNER,
                }
            ),
            encoding="utf-8",
        )
        status_path.chmod(0o644)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "scheduler runtime state is invalid",
        ):
            MODULE._read_scheduler_runtime_state(
                self.home,
                recover_timestamps=True,
            )

    def test_audit_and_doctor_detect_skill_issues_without_deletion(self) -> None:
        duplicate_one = self.write_skill("duplicate-one", "duplicate-name")
        duplicate_two = self.write_skill("duplicate-two", "duplicate-name")
        managed_drift = self.write_skill("managed-drift", "managed-name")
        cache_entry = self.home / "skills" / ".cache"
        cache_entry.mkdir()
        backup_entry = self.home / "skills" / "bug-triage-playbook.bak-20260312-145916"
        backup_entry.mkdir()
        system_backup_entry = (
            self.home / "skills" / ".system" / "legacy-system-skill.bak-20260723"
        )
        system_backup_entry.mkdir(parents=True)
        broken_link = self.home / "skills" / "broken-link"
        broken_link.symlink_to(self.root / "missing-skill", target_is_directory=True)
        record = MODULE.ManagedLinkRecord(
            source=PurePosixPath("personal_codex/skills/managed-drift"),
            target=PurePosixPath("skills/managed-drift"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            link_target="../personal-sync/releases/expected/managed-drift",
            release_sha=PUBLIC_SHA,
        )
        managed_state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: PUBLIC_SHA},
            links={record.target: record},
        )
        healthy_report = MODULE.SchedulerReport(
            platform="linux",
            installed=True,
            enabled=True,
            config_paths=(self.root / "scheduler.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            stable_runner=True,
            mode="public",
            base_repo="owner/public-sync",
            private_repo=None,
            last_attempt=None,
            recent_success=None,
            current_releases=(),
            failure_reason=None,
        )
        before = snapshot_tree(self.home / "skills")

        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            return_value=managed_state,
        ):
            audit_issues = MODULE.audit_active_skills(self.home)
        audit_codes = {issue.code for issue in audit_issues}
        self.assertTrue(
            {
                "unmanaged-skill",
                "broken-link",
                "duplicate-skill-name",
                "cache-or-backup",
                "generated-drift",
            }.issubset(audit_codes)
        )
        self.assertEqual(
            {issue.path for issue in audit_issues if issue.code == "unmanaged-skill"},
            {duplicate_one, duplicate_two},
        )
        self.assertEqual(
            {
                issue.path
                for issue in audit_issues
                if issue.code == "duplicate-skill-name"
            },
            {duplicate_one, duplicate_two},
        )
        self.assertEqual(
            {issue.path for issue in audit_issues if issue.code == "cache-or-backup"},
            {cache_entry, backup_entry, system_backup_entry},
        )
        self.assertIn(
            ("broken-link", broken_link),
            {(issue.code, issue.path) for issue in audit_issues},
        )
        self.assertIn(
            ("generated-drift", managed_drift),
            {(issue.code, issue.path) for issue in audit_issues},
        )
        self.assertEqual(snapshot_tree(self.home / "skills"), before)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_load_managed_state",
                return_value=managed_state,
            ),
            mock.patch.object(
                MODULE,
                "scheduler_report",
                return_value=healthy_report,
            ),
            contextlib.redirect_stdout(output),
        ):
            report, doctor_issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        self.assertIs(report, healthy_report)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            {issue["code"] for issue in payload["issues"]},
            {issue.code for issue in doctor_issues},
        )
        self.assertTrue(
            {
                "unmanaged-skill",
                "broken-link",
                "duplicate-skill-name",
                "cache-or-backup",
                "generated-drift",
            }.issubset({issue.code for issue in doctor_issues})
        )
        self.assertEqual(snapshot_tree(self.home / "skills"), before)

    def test_macos_loader_rejects_program_override(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        payload = MODULE._launchd_plist(
            self.home,
            "owner/public-sync",
            19,
            runner,
        )
        payload["Program"] = "/tmp/attacker"
        paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported execution semantics",
        ):
            MODULE._load_macos_scheduler_config(paths)

    def test_linux_loader_rejects_extra_execution_semantics_and_dropins(
        self,
    ) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        service = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        ).replace(
            "ExecStart=",
            'ExecStartPre="/tmp/attacker"\nExecStart=',
        )
        paths.systemd_service.write_text(service, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(23),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported execution semantics",
        ):
            MODULE._load_linux_scheduler_config(paths)

        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        drop_in = paths.systemd_service.with_name(paths.systemd_service.name + ".d")
        drop_in.mkdir()
        (drop_in / "override.conf").write_text(
            "[Service]\nExecStartPre=/tmp/attacker\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "drop-ins are unsupported",
        ):
            MODULE._load_linux_scheduler_config(paths)

    def test_systemd_exec_arguments_preserve_literal_special_paths(self) -> None:
        runner = (
            self.home / "bin with spaces" / 'runner %h $RUNNER "quoted" \\ backslash'
        )
        expected = MODULE._scheduler_install_args(
            runner,
            "owner/public-sync",
            self.home,
        )
        service = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        )
        exec_start = next(
            line.partition("=")[2]
            for line in service.splitlines()
            if line.startswith("ExecStart=")
        )

        self.assertIn("%%h", exec_start)
        self.assertIn("$$RUNNER", exec_start)
        self.assertIn('\\"quoted\\"', exec_start)
        self.assertIn("\\\\ backslash", exec_start)
        self.assertEqual(
            MODULE._parse_systemd_exec_arguments(exec_start),
            expected,
        )

    def test_linux_loader_decodes_exact_systemd_runtime_arguments(self) -> None:
        runner = (
            self.home / "bin with spaces" / 'runner %h $RUNNER "quoted" \\ backslash'
        )
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        service = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        )
        paths.systemd_service.write_text(service, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(23),
            encoding="utf-8",
        )

        config = MODULE._load_linux_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.repo, "owner/public-sync")

    def test_systemd_exec_parser_rejects_expansion_and_shell_forms(self) -> None:
        commands = {
            "specifier": '"/absolute/runner%h" "run-scheduled"',
            "variable": '"/absolute/$RUNNER" "run-scheduled"',
            "single-quote": "'/absolute/runner' 'run-scheduled'",
            "backslash-escape": '"/absolute/runner\\sname" "run-scheduled"',
        }
        for name, command in commands.items():
            with self.subTest(name=name), self.assertRaises(MODULE.SyncError):
                MODULE._parse_systemd_exec_arguments(command)

    def test_systemd_arguments_reject_controls_and_invalid_utf8(self) -> None:
        rejected = {
            "newline": "\n",
            "nul": "\0",
            "tab": "\t",
            "delete": "\x7f",
            "c1-control": "\x85",
            "surrogate": "\udcff",
        }
        for name, character in rejected.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "valid UTF-8|control characters",
                ),
            ):
                MODULE._systemd_quote(f"/absolute/runner{character}unsafe")

    def test_linux_install_rejects_control_path_before_transaction(self) -> None:
        runner = self.home / "bin" / "runner\nunsafe"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        with (
            mock.patch.object(MODULE, "_install_scheduler_transaction") as install,
            self.assertRaisesRegex(MODULE.SyncError, "control characters"),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                23,
                "linux",
                str(runner),
                dry_run=False,
                enable=False,
            )
        install.assert_not_called()

    def test_linux_interval_parser_is_bounded(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        timer = MODULE._systemd_timer(1).replace(
            "OnUnitActiveSec=1min",
            f"OnUnitActiveSec={'9' * 4301}min",
        )
        paths.systemd_timer.write_text(timer, encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "interval must use whole minutes",
        ):
            MODULE._load_linux_scheduler_config(paths)

    def test_install_rejects_invalid_inputs_before_writing(self) -> None:
        self.write_runner()
        with (
            mock.patch.object(MODULE, "_write_text") as write_text,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "owner/repo",
            ),
        ):
            MODULE.install_scheduler(
                self.home,
                "not-a-repository",
                30,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )
        write_text.assert_not_called()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "must not exceed",
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/repository",
                MODULE.MAX_SCHEDULER_INTERVAL_MINUTES + 1,
                "linux",
                None,
                dry_run=True,
                enable=False,
            )

    def test_runner_validation_rejects_relative_and_directory_paths(self) -> None:
        with self.assertRaisesRegex(MODULE.SyncError, "absolute path"):
            MODULE._validate_scheduler_runner(
                Path("relative-runner"),
                dry_run=True,
            )
        runner_directory = self.home / "bin" / "runner-directory"
        runner_directory.mkdir(parents=True)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "regular executable",
        ):
            MODULE._validate_scheduler_runner(
                runner_directory,
                dry_run=False,
            )

    def test_stable_runner_requires_managed_symlink_claim(self) -> None:
        plain_runner = self.write_runner()
        config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "timer",),
            interval_minutes=60,
            runner=plain_runner,
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertFalse(MODULE._stable_scheduler_runner_matches(self.home, config))

        plain_runner.unlink()
        managed_runner = (
            self.home
            / "personal-sync"
            / "releases"
            / PUBLIC_SHA
            / "scripts"
            / "codex_personal_sync.py"
        )
        managed_runner.parent.mkdir(parents=True)
        managed_runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_runner.chmod(0o755)
        link_target = os.path.relpath(managed_runner, plain_runner.parent)
        plain_runner.symlink_to(link_target)
        record = MODULE.ManagedLinkRecord(
            source=PurePosixPath("scripts/codex_personal_sync.py"),
            target=PurePosixPath("bin/codex-personal-sync"),
            kind="file",
            owner=MODULE.PUBLIC_OWNER,
            link_target=link_target,
            release_sha=PUBLIC_SHA,
        )
        state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: PUBLIC_SHA},
            links={record.target: record},
        )
        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            return_value=state,
        ):
            self.assertTrue(MODULE._stable_scheduler_runner_matches(self.home, config))

        alternate = self.home / "alternate-runner"
        alternate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        alternate.chmod(0o755)

        def replace_link_then_return_state(
            _home: Path,
        ) -> MODULE.ManagedState:
            plain_runner.unlink()
            plain_runner.symlink_to(os.path.relpath(alternate, plain_runner.parent))
            return state

        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            side_effect=replace_link_then_return_state,
        ):
            self.assertFalse(MODULE._stable_scheduler_runner_matches(self.home, config))

    def test_runtime_target_mismatch_is_unhealthy_and_not_carried_forward(
        self,
    ) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/current",
                runner,
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(31),
            encoding="utf-8",
        )
        stale_runtime = {
            "version": 1,
            "last_attempt": "2026-07-23T08:00:00+00:00",
            "last_success": "2026-07-23T08:00:00+00:00",
            "success": False,
            "failure_reason": "old target failed",
            "mode": "public",
            "repo": "owner/old",
            "base_repo": "owner/old",
            "owner": MODULE.PUBLIC_OWNER,
        }
        with mock.patch.object(
            MODULE,
            "_read_scheduler_runtime_state",
            return_value=stale_runtime,
        ):
            report = MODULE.scheduler_report(self.home, "linux")
        self.assertEqual(
            report.failure_reason,
            "scheduler runtime state belongs to a different configured target",
        )
        self.assertEqual(report.failure_code, "scheduler-target-mismatch")
        self.assertIsNone(report.last_attempt)
        self.assertIsNone(report.recent_success)
        next_payload = MODULE._scheduler_runtime_payload(
            previous=stale_runtime,
            attempt="2026-07-23T09:00:00+00:00",
            success=False,
            failure_reason="failed",
            mode="public",
            repo="owner/current",
            base_repo="owner/current",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertIsNone(next_payload["last_success"])

    def test_linux_pair_transaction_recovers_crash_and_preserves_interval(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/old",
            17,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        real_write = MODULE._write_text_with_activation_binding
        injected = False

        def fail_before_timer(
            path: Path,
            content: str,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot,
            description: str,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.SchedulerActivationBinding,
        ]:
            nonlocal injected
            if path == paths.systemd_timer and not injected:
                injected = True
                raise MODULE.SyncError("injected pair crash")
            return real_write(
                path,
                content,
                expected_snapshot=expected_snapshot,
                description=description,
            )

        with (
            mock.patch.object(
                MODULE,
                "_write_text_with_activation_binding",
                side_effect=fail_before_timer,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "pair crash"),
        ):
            self.install_scheduler_quietly(
                "owner/new",
                None,
                "linux",
            )

        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())
        self.install_scheduler_quietly(
            "owner/new",
            None,
            "linux",
        )
        self.assertFalse(MODULE._scheduler_pair_transaction_path(paths).exists())
        config = MODULE._load_linux_scheduler_config(paths)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.repo, "owner/new")
        self.assertEqual(config.interval_minutes, 17)

    def test_concurrent_linux_installs_serialize_recovery_through_cleanup(
        self,
    ) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        first_publication_paused = threading.Event()
        release_first_publication = threading.Event()
        second_install_started = threading.Event()
        second_recovery_entered = threading.Event()
        errors: dict[str, BaseException] = {}
        real_write = MODULE._write_text_with_activation_binding
        real_recover = MODULE._recover_scheduler_pair_transaction

        def pause_first_publication(
            path: Path,
            content: str,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot,
            description: str,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.SchedulerActivationBinding,
        ]:
            if (
                threading.current_thread().name == "first-scheduler-install"
                and path == paths.systemd_service
            ):
                first_publication_paused.set()
                if not release_first_publication.wait(5):
                    raise AssertionError("first scheduler publication was not released")
            return real_write(
                path,
                content,
                expected_snapshot=expected_snapshot,
                description=description,
            )

        def observe_recovery(
            selected_paths: MODULE.SchedulerPaths,
            *,
            dry_run: bool,
        ) -> bool:
            if threading.current_thread().name == "second-scheduler-install":
                second_recovery_entered.set()
            return real_recover(selected_paths, dry_run=dry_run)

        def install(name: str, repo: str, interval: int) -> None:
            if name == "second":
                second_install_started.set()
            try:
                MODULE.install_scheduler(
                    self.home,
                    repo,
                    interval,
                    "linux",
                    None,
                    dry_run=False,
                    enable=False,
                )
            except BaseException as error:
                errors[name] = error

        first = threading.Thread(
            target=install,
            args=("first", "owner/first", 17),
            name="first-scheduler-install",
            daemon=True,
        )
        second = threading.Thread(
            target=install,
            args=("second", "owner/second", 29),
            name="second-scheduler-install",
            daemon=True,
        )
        with (
            mock.patch.object(
                MODULE,
                "_write_text_with_activation_binding",
                side_effect=pause_first_publication,
            ),
            mock.patch.object(
                MODULE,
                "_recover_scheduler_pair_transaction",
                side_effect=observe_recovery,
            ),
        ):
            try:
                first.start()
                self.assertTrue(first_publication_paused.wait(5))
                self.assertTrue(
                    MODULE._scheduler_pair_transaction_path(paths).is_file()
                )

                second.start()
                self.assertTrue(second_install_started.wait(5))
                self.assertFalse(second_recovery_entered.wait(0.2))
                self.assertTrue(
                    MODULE._scheduler_pair_transaction_path(paths).is_file()
                )
            finally:
                release_first_publication.set()
                if first.ident is not None:
                    first.join(5)
                if second.ident is not None:
                    second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, {})
        self.assertTrue(second_recovery_entered.is_set())
        self.assertFalse(MODULE._scheduler_pair_transaction_path(paths).exists())
        config = MODULE._load_linux_scheduler_config(paths)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.repo, "owner/second")
        self.assertEqual(config.interval_minutes, 29)

    def test_linux_pair_transaction_refuses_concurrent_editor_state(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "linux")
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        original_timer = paths.systemd_timer.read_bytes()
        real_write = MODULE._write_text_with_activation_binding
        injected = False

        def edit_before_conditional_write(
            path: Path,
            content: str,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot,
            description: str,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.SchedulerActivationBinding,
        ]:
            nonlocal injected
            if path == paths.systemd_service and not injected:
                injected = True
                path.write_text("user edit\n", encoding="utf-8")
                path.chmod(0o600)
            return real_write(
                path,
                content,
                expected_snapshot=expected_snapshot,
                description=description,
            )

        with (
            mock.patch.object(
                MODULE,
                "_write_text_with_activation_binding",
                side_effect=edit_before_conditional_write,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")

        self.assertEqual(
            paths.systemd_service.read_text(encoding="utf-8"),
            "user edit\n",
        )
        self.assertEqual(paths.systemd_timer.read_bytes(), original_timer)
        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "service changed during pending pair transaction",
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")
        self.assertEqual(
            paths.systemd_service.read_text(encoding="utf-8"),
            "user edit\n",
        )

    def test_systemd_pair_recovery_retains_marker_on_member_replacement(
        self,
    ) -> None:
        real_parse = MODULE._parse_scheduler_pair_transaction
        for target_kind in ("service", "timer"):
            with self.subTest(target=target_kind):
                case_user_home = self.root / f"pair-replacement-{target_kind}" / "home"
                case_home = case_user_home / ".codex"
                paths = self.write_pending_systemd_pair(
                    case_user_home,
                    case_home,
                )
                target = (
                    paths.systemd_service
                    if target_kind == "service"
                    else paths.systemd_timer
                )
                assert target is not None
                marker = MODULE._scheduler_pair_transaction_path(paths)
                replaced = False

                def parse_then_replace(
                    payload: bytes,
                    path: Path,
                ) -> tuple[
                    MODULE.ManagedStateFileSnapshot,
                    MODULE.ManagedStateFileSnapshot,
                    bytes,
                    bytes,
                ]:
                    nonlocal replaced
                    parsed = real_parse(payload, path)
                    replacement = target.with_name(target.name + ".replacement")
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                    os.replace(replacement, target)
                    replaced = True
                    return parsed

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_parse_scheduler_pair_transaction",
                        side_effect=parse_then_replace,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "recovery object group changed",
                    ),
                ):
                    MODULE._recover_scheduler_pair_transaction(
                        paths,
                        dry_run=False,
                    )

                self.assertTrue(replaced)
                self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_revalidates_absent_group_after_parent_sync(
        self,
    ) -> None:
        for mutation in ("during-fsync", "during-second-pass"):
            with self.subTest(mutation=mutation):
                case_user_home = self.root / f"pair-absent-{mutation}" / "home"
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths("linux", case_home)
                assert paths.systemd_service is not None
                assert paths.systemd_timer is not None
                paths.systemd_service.parent.mkdir(parents=True)
                marker = MODULE._scheduler_pair_transaction_path(paths)
                absent = MODULE.ManagedStateFileSnapshot(exists=False)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    marker_before = MODULE._scheduler_config_snapshot(
                        marker,
                        MODULE.MAX_SCHEDULER_PAIR_TRANSACTION_BYTES,
                    )
                    MODULE._atomic_write_scheduler_config(
                        marker,
                        MODULE._scheduler_pair_transaction_payload(
                            service_before=absent,
                            timer_before=absent,
                            service_after=b"future service\n",
                            timer_after=b"future timer\n",
                        ),
                        expected_snapshot=marker_before,
                    )

                real_fsync = MODULE.os.fsync
                real_member_check = MODULE._revalidate_systemd_pair_recovery_member
                armed = False
                injected = False
                timer_checks = 0

                def reappear_service() -> None:
                    nonlocal injected
                    paths.systemd_service.write_bytes(b"concurrent service\n")
                    paths.systemd_service.chmod(0o600)
                    injected = True

                def sync_then_arm_or_reappear(file_fd: int) -> None:
                    nonlocal armed
                    real_fsync(file_fd)
                    if mutation == "during-fsync":
                        reappear_service()
                    else:
                        armed = True

                def reappear_after_earlier_absence_check(
                    group: MODULE.SystemdPairRecoveryGroup,
                    member: MODULE.SystemdPairRecoveryMember,
                ) -> None:
                    nonlocal timer_checks
                    if (
                        mutation == "during-second-pass"
                        and armed
                        and member.path == paths.systemd_timer
                    ):
                        timer_checks += 1
                        if timer_checks == 2:
                            reappear_service()
                    real_member_check(group, member)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "fsync",
                        side_effect=sync_then_arm_or_reappear,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_revalidate_systemd_pair_recovery_member",
                        side_effect=reappear_after_earlier_absence_check,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "recovery object group changed",
                    ) as raised,
                ):
                    MODULE._recover_scheduler_pair_transaction(
                        paths,
                        dry_run=False,
                    )

                self.assertTrue(injected)
                self.assertIn(str(paths.systemd_service), str(raised.exception))
                self.assertIn(
                    "after parent sync before transaction marker commit",
                    str(raised.exception),
                )
                self.assertTrue(paths.systemd_service.is_file())
                self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_rechecks_earlier_member_after_later_member(
        self,
    ) -> None:
        case_user_home = self.root / "pair-later-member-race" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        marker = MODULE._scheduler_pair_transaction_path(paths)
        real_parse = MODULE._parse_scheduler_pair_transaction
        real_matches = MODULE._scheduler_recovery_binding_matches
        armed = False
        replaced = False

        def parse_and_arm(
            payload: bytes,
            path: Path,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.ManagedStateFileSnapshot,
            bytes,
            bytes,
        ]:
            nonlocal armed
            parsed = real_parse(payload, path)
            armed = True
            return parsed

        def replace_service_while_timer_is_checked(
            home: Path,
            file_fd: int,
            path: Path,
            parent_fd: int,
            expected: MODULE.ManagedStateFileSnapshot,
        ) -> bool:
            nonlocal replaced
            if armed and not replaced and path == paths.systemd_timer:
                replacement = paths.systemd_service.with_name(
                    paths.systemd_service.name + ".replacement"
                )
                replacement.write_bytes(paths.systemd_service.read_bytes())
                replacement.chmod(stat.S_IMODE(paths.systemd_service.stat().st_mode))
                os.replace(replacement, paths.systemd_service)
                replaced = True
            return real_matches(
                home,
                file_fd,
                path,
                parent_fd,
                expected,
            )

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_parse_scheduler_pair_transaction",
                side_effect=parse_and_arm,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_recovery_binding_matches",
                side_effect=replace_service_while_timer_is_checked,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "recovery object group changed",
            ),
        ):
            MODULE._recover_scheduler_pair_transaction(
                paths,
                dry_run=False,
            )

        self.assertTrue(replaced)
        self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_closes_partial_bindings(self) -> None:
        case_user_home = self.root / "pair-partial-bind" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        real_bind = MODULE._bind_systemd_pair_recovery_member
        bound_fds: list[int] = []

        def bind_marker_then_fail(
            home: Path,
            path: Path,
            parent_fd: int,
            *,
            maximum_bytes: int = 1024 * 1024,
        ) -> MODULE.SystemdPairRecoveryMember:
            if path == paths.systemd_service:
                raise MODULE.SyncError("simulated service binding failure")
            member = real_bind(
                home,
                path,
                parent_fd,
                maximum_bytes=maximum_bytes,
            )
            if member.file_fd >= 0:
                bound_fds.append(member.file_fd)
            return member

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_bind_systemd_pair_recovery_member",
                side_effect=bind_marker_then_fail,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "simulated service binding failure",
            ),
        ):
            with MODULE._retain_systemd_pair_recovery_group(paths):
                self.fail("partial recovery binding unexpectedly succeeded")

        self.assertTrue(bound_fds)
        for file_fd in bound_fds:
            with self.assertRaises(OSError):
                os.fstat(file_fd)

    def test_systemd_pair_recovery_retains_marker_across_parent_aba(
        self,
    ) -> None:
        case_user_home = self.root / "pair-parent-aba" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        unit_parent = paths.systemd_service.parent
        displaced_parent = unit_parent.with_name(unit_parent.name + ".displaced")
        transient_parent = unit_parent.with_name(unit_parent.name + ".transient")
        marker = MODULE._scheduler_pair_transaction_path(paths)
        real_parse = MODULE._parse_scheduler_pair_transaction
        rotated = False

        def parse_then_rotate_parent(
            payload: bytes,
            path: Path,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.ManagedStateFileSnapshot,
            bytes,
            bytes,
        ]:
            nonlocal rotated
            parsed = real_parse(payload, path)
            unit_parent.rename(displaced_parent)
            unit_parent.mkdir()
            for source in displaced_parent.iterdir():
                if not source.is_file():
                    continue
                destination = unit_parent / source.name
                destination.write_bytes(source.read_bytes())
                destination.chmod(stat.S_IMODE(source.stat().st_mode))
            rotated = True
            return parsed

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_parse_scheduler_pair_transaction",
                side_effect=parse_then_rotate_parent,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "recovery object group changed",
            ),
        ):
            MODULE._recover_scheduler_pair_transaction(
                paths,
                dry_run=False,
            )

        self.assertTrue(rotated)
        self.assertTrue((displaced_parent / marker.name).is_file())
        unit_parent.rename(transient_parent)
        displaced_parent.rename(unit_parent)
        self.assertTrue(marker.is_file())
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(
                MODULE._recover_scheduler_pair_transaction(
                    paths,
                    dry_run=False,
                )
            )
        self.assertFalse(marker.exists())

    def test_macos_install_binds_semantically_audited_snapshot(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "macos")
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        real_audit = MODULE._audit_scheduler_config
        replacement = b"user edited launchd config\n"

        def audit_then_edit(
            selected_paths: MODULE.SchedulerPaths,
        ) -> MODULE.SchedulerConfigAudit:
            audit = real_audit(selected_paths)
            paths.launchd_plist.write_bytes(replacement)
            paths.launchd_plist.chmod(0o600)
            return audit

        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                side_effect=audit_then_edit,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "macos")

        self.assertEqual(paths.launchd_plist.read_bytes(), replacement)

    def test_macos_install_revalidates_bound_plist_at_every_native_boundary(
        self,
    ) -> None:
        action_names = (
            "legacy-bootout",
            "legacy-disable",
            "current-bootout",
            "bootstrap",
            "enable",
        )
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        for action_index, action_name in enumerate(action_names):
            for mutation, expected_error in mutations:
                with self.subTest(action=action_name, mutation=mutation):
                    case_user_home = self.root / f"{action_index}-{mutation}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    paths = MODULE.SchedulerPaths(
                        platform="macos",
                        launchd_plist=(
                            case_user_home
                            / "Library"
                            / "LaunchAgents"
                            / f"{MODULE.LAUNCHD_LABEL}.plist"
                        ),
                    )
                    assert paths.launchd_plist is not None
                    native_calls = 0
                    force_unreadable = False
                    real_read = MODULE._read_managed_state_bytes

                    def mutate_during_native(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal force_unreadable, native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        plist = paths.launchd_plist
                        if mutation == "replacement":
                            replacement = plist.with_name(plist.name + ".replacement")
                            replacement.write_bytes(plist.read_bytes())
                            replacement.chmod(0o600)
                            os.replace(replacement, plist)
                        elif mutation == "content":
                            payload = bytearray(plist.read_bytes())
                            payload[len(payload) // 2] ^= 1
                            plist.write_bytes(payload)
                            plist.chmod(0o600)
                        elif mutation == "mode":
                            plist.chmod(0o644)
                        elif mutation == "parent":
                            old_parent = plist.parent.with_name(
                                plist.parent.name + ".replaced"
                            )
                            plist.parent.rename(old_parent)
                            plist.parent.mkdir()
                            replacement = plist.parent / plist.name
                            replacement.write_bytes(
                                (old_parent / plist.name).read_bytes()
                            )
                            replacement.chmod(0o600)
                        elif mutation == "missing":
                            plist.unlink()
                        else:
                            force_unreadable = True

                    def fail_bound_read(
                        file_fd: int,
                        path: Path,
                        maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
                    ) -> bytes:
                        if force_unreadable and path == paths.launchd_plist:
                            raise MODULE.SyncError("injected read failure")
                        return real_read(file_fd, path, maximum_bytes)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_during_native,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_read_managed_state_bytes",
                            side_effect=fail_bound_read,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    self.assertNotIn(
                        "installed macOS launchd scheduler",
                        output.getvalue(),
                    )

    def test_macos_install_prebinds_every_legacy_before_cleanup(self) -> None:
        case_user_home = self.root / "install-all-legacy-bindings" / "home"
        case_home = case_user_home / ".codex"
        runner = case_home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        labels = (
            "com.joeyteng.codex-personal-sync.one",
            "com.joeyteng.codex-personal-sync.two",
        )
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "LEGACY_LAUNCHD_LABELS",
                labels,
            ),
        ):
            paths = MODULE._scheduler_paths("macos", case_home)
            second_legacy = MODULE._legacy_launchd_plist(paths, labels[1])
        second_legacy.parent.mkdir(parents=True)
        second_legacy.write_bytes(b"original second legacy\n")
        second_legacy.chmod(0o600)
        replacement = b"replacement second legacy\n"
        native_calls = 0

        def replace_second_legacy_during_first_cleanup_action(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            nonlocal native_calls
            native_calls += 1
            candidate = second_legacy.with_name(second_legacy.name + ".replacement")
            candidate.write_bytes(replacement)
            candidate.chmod(0o600)
            os.replace(candidate, second_legacy)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "LEGACY_LAUNCHD_LABELS",
                labels,
            ),
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=replace_second_legacy_during_first_cleanup_action,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "object identity changed",
            ),
        ):
            MODULE.install_scheduler(
                case_home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(native_calls, 1)
        self.assertEqual(second_legacy.read_bytes(), replacement)
        assert paths.launchd_plist is not None
        self.assertTrue(paths.launchd_plist.exists())
        self.assertNotIn(
            "installed macOS launchd scheduler",
            output.getvalue(),
        )

    def test_macos_install_legacy_cleanup_native_failures_stop_migration(
        self,
    ) -> None:
        success = subprocess.CompletedProcess(
            ["scheduler-action"],
            0,
            "",
            "",
        )
        failures: tuple[
            tuple[str, int, BaseException | subprocess.CompletedProcess[str], str],
            ...,
        ] = (
            (
                "timeout",
                0,
                MODULE.SyncError(
                    "scheduler native command exceeded its monotonic deadline",
                    code="scheduler-timeout",
                ),
                "failed to run launchctl bootout",
            ),
            (
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["launchctl", "bootout"],
                    1,
                    "",
                    "Operation not permitted",
                ),
                "Operation not permitted",
            ),
            (
                "unknown",
                1,
                subprocess.CompletedProcess(
                    ["launchctl", "disable"],
                    1,
                    "",
                    "Input/output error",
                ),
                "Input/output error",
            ),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for failure_kind, failure_call, failure, expected_error in failures:
            with self.subTest(failure=failure_kind):
                case_user_home = self.root / f"install-legacy-{failure_kind}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths("macos", case_home)
                assert paths.launchd_plist is not None
                legacy = MODULE._legacy_launchd_plist(paths, label)
                legacy.parent.mkdir(parents=True)
                legacy_payload = b"legacy scheduler config\n"
                legacy.write_bytes(legacy_payload)
                legacy.chmod(0o600)
                results: list[BaseException | subprocess.CompletedProcess[str]] = [
                    success
                ] * failure_call + [failure]
                native_calls: list[list[str]] = []

                def run_native(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    native_calls.append(args)
                    result = results.pop(0)
                    if isinstance(result, BaseException):
                        raise result
                    return result

                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=run_native,
                    ),
                    contextlib.redirect_stdout(output),
                    self.assertRaisesRegex(MODULE.SyncError, expected_error),
                ):
                    MODULE.install_scheduler(
                        case_home,
                        "owner/public-sync",
                        17,
                        "macos",
                        None,
                        dry_run=False,
                        enable=True,
                    )

                self.assertEqual(results, [])
                self.assertEqual(len(native_calls), failure_call + 1)
                self.assertEqual(native_calls[0][1], "bootout")
                if failure_call:
                    self.assertEqual(native_calls[1][1], "disable")
                self.assertFalse(
                    any(
                        len(args) > 1 and args[1] in {"bootstrap", "enable"}
                        for args in native_calls
                    )
                )
                self.assertEqual(legacy.read_bytes(), legacy_payload)
                self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o600)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    config = MODULE._load_macos_scheduler_config(paths)
                self.assertIsNotNone(config)
                assert config is not None
                self.assertEqual(config.repo, "owner/public-sync")
                self.assertNotIn(
                    "installed macOS launchd scheduler",
                    output.getvalue(),
                )

    def test_macos_install_legacy_cleanup_accepts_precise_absence(
        self,
    ) -> None:
        case_user_home = self.root / "install-legacy-already-absent" / "home"
        case_home = case_user_home / ".codex"
        runner = case_home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        with mock.patch.object(
            MODULE.Path,
            "home",
            return_value=case_user_home,
        ):
            paths = MODULE._scheduler_paths("macos", case_home)
        assert paths.launchd_plist is not None
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        legacy = MODULE._legacy_launchd_plist(paths, label)
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy scheduler config\n")
        legacy.chmod(0o600)
        results = [
            subprocess.CompletedProcess(
                ["launchctl", "bootout"],
                1,
                "",
                "Boot-out failed: 3: No such process",
            ),
            subprocess.CompletedProcess(
                ["launchctl", "disable"],
                1,
                "",
                "Could not find specified service",
            ),
            *(
                subprocess.CompletedProcess(
                    ["scheduler-action"],
                    0,
                    "",
                    "",
                )
                for _ in range(3)
            ),
        ]
        native_calls: list[list[str]] = []

        def run_native(
            args: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            native_calls.append(args)
            return results.pop(0)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=run_native,
            ),
            contextlib.redirect_stdout(output),
        ):
            MODULE.install_scheduler(
                case_home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(results, [])
        self.assertEqual(
            [args[1] for args in native_calls],
            ["bootout", "disable", "bootout", "bootstrap", "enable"],
        )
        self.assertFalse(legacy.exists())
        with mock.patch.object(
            MODULE.Path,
            "home",
            return_value=case_user_home,
        ):
            config = MODULE._load_macos_scheduler_config(paths)
        self.assertIsNotNone(config)
        self.assertEqual(
            output.getvalue().count("ignored already-absent scheduler command"),
            2,
        )
        self.assertIn("installed macOS launchd scheduler", output.getvalue())

    def test_macos_install_retains_legacy_absence_through_current_actions(
        self,
    ) -> None:
        legacy_action_count = len(MODULE.LEGACY_LAUNCHD_LABELS) * 2
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        case_index = 0
        for initial_legacy_exists in (False, True):
            for current_action_offset in range(3):
                case_index += 1
                with self.subTest(
                    initial_legacy_exists=initial_legacy_exists,
                    current_action=current_action_offset,
                ):
                    case_user_home = (
                        self.root / f"install-legacy-retained-{case_index}" / "home"
                    )
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths("macos", case_home)
                    assert paths.launchd_plist is not None
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    legacy.parent.mkdir(parents=True)
                    if initial_legacy_exists:
                        legacy.write_bytes(b"original legacy scheduler\n")
                        legacy.chmod(0o600)
                    payload = (
                        f"new legacy {initial_legacy_exists} {current_action_offset}\n"
                    ).encode("utf-8")
                    mutation_action = legacy_action_count + current_action_offset
                    native_calls = 0

                    def reappear_during_current_action(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != mutation_action:
                            return
                        legacy.write_bytes(payload)
                        legacy.chmod(0o600)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=reappear_during_current_action,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            (
                                "reappeared after conditional removal"
                                if initial_legacy_exists
                                else "appeared after initial absence"
                            ),
                        ),
                    ):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertEqual(native_calls, mutation_action + 1)
                    self.assertEqual(legacy.read_bytes(), payload)
                    self.assertTrue(paths.launchd_plist.exists())
                    self.assertNotIn(
                        "installed macOS launchd scheduler",
                        output.getvalue(),
                    )

    def test_macos_install_allows_mtime_only_churn_at_native_boundaries(
        self,
    ) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        native_calls = 0

        def touch_during_native(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            nonlocal native_calls
            native_calls += 1
            metadata = paths.launchd_plist.stat()
            os.utime(
                paths.launchd_plist,
                ns=(
                    metadata.st_atime_ns,
                    metadata.st_mtime_ns + 1_000_000,
                ),
            )

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=touch_during_native,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            native_calls,
            len(MODULE.LEGACY_LAUNCHD_LABELS) * 2 + 3,
        )
        self.assertIsNotNone(MODULE._load_macos_scheduler_config(paths))

    def test_legacy_launchd_cleanup_binds_file_across_native_actions(
        self,
    ) -> None:
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for mutation, expected_error in mutations:
            with self.subTest(mutation=mutation):
                case_user_home = self.root / f"legacy-{mutation}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                paths = MODULE.SchedulerPaths(
                    platform="macos",
                    launchd_plist=(
                        case_user_home
                        / "Library"
                        / "LaunchAgents"
                        / f"{MODULE.LAUNCHD_LABEL}.plist"
                    ),
                )
                legacy = MODULE._legacy_launchd_plist(paths, label)
                legacy.parent.mkdir(parents=True)
                original = b"legacy launchd config\n"
                replacement = b"user replacement config\n"
                legacy.write_bytes(original)
                legacy.chmod(0o600)
                force_unreadable = False
                real_read = MODULE._read_managed_state_bytes

                def mutate_legacy(
                    _args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool = False,
                ) -> None:
                    del dry_run, allow_fail
                    nonlocal force_unreadable
                    if mutation == "replacement":
                        candidate = legacy.with_name(legacy.name + ".replacement")
                        candidate.write_bytes(replacement)
                        candidate.chmod(0o600)
                        os.replace(candidate, legacy)
                    elif mutation == "content":
                        legacy.write_bytes(replacement)
                        legacy.chmod(0o600)
                    elif mutation == "mode":
                        legacy.chmod(0o644)
                    elif mutation == "parent":
                        displaced_parent = legacy.parent.with_name(
                            legacy.parent.name + ".displaced"
                        )
                        legacy.parent.rename(displaced_parent)
                        legacy.parent.mkdir()
                        candidate = legacy.parent / legacy.name
                        candidate.write_bytes(replacement)
                        candidate.chmod(0o600)
                    elif mutation == "missing":
                        legacy.unlink()
                    else:
                        force_unreadable = True

                def fail_legacy_read(
                    file_fd: int,
                    path: Path,
                    maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
                ) -> bytes:
                    if force_unreadable and path == legacy:
                        raise MODULE.SyncError("injected legacy read failure")
                    return real_read(file_fd, path, maximum_bytes)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=mutate_legacy,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_managed_state_bytes",
                        side_effect=fail_legacy_read,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(MODULE.SyncError, expected_error),
                ):
                    MODULE.install_scheduler(
                        case_home,
                        "owner/public-sync",
                        17,
                        "macos",
                        None,
                        dry_run=False,
                        enable=True,
                    )

                if mutation in {"replacement", "content"}:
                    self.assertEqual(legacy.read_bytes(), replacement)
                elif mutation == "mode":
                    self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o644)
                elif mutation == "parent":
                    displaced = legacy.parent.with_name(
                        legacy.parent.name + ".displaced"
                    )
                    self.assertEqual(
                        (displaced / legacy.name).read_bytes(),
                        original,
                    )
                    self.assertEqual(legacy.read_bytes(), replacement)
                elif mutation == "missing":
                    self.assertFalse(legacy.exists())
                else:
                    self.assertEqual(legacy.read_bytes(), original)

    def test_legacy_launchd_cleanup_allows_mtime_only_churn(self) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        legacy = MODULE._legacy_launchd_plist(paths, label)
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy launchd config\n")
        legacy.chmod(0o600)

        def touch_legacy(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            if legacy.exists():
                metadata = legacy.stat()
                os.utime(
                    legacy,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000,
                    ),
                )

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=touch_legacy,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertFalse(legacy.exists())

    def test_absent_legacy_launchd_cleanup_retains_parent_and_absence(self) -> None:
        mutations = (
            ("appearance", "appeared after initial absence"),
            ("parent", "parent chain changed"),
            ("unreadable", "parent descriptor became unreadable"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for action_index in range(2):
            for mutation, expected_error in mutations:
                with self.subTest(action=action_index, mutation=mutation):
                    case_user_home = (
                        self.root / f"legacy-absent-{action_index}-{mutation}" / "home"
                    )
                    paths = MODULE.SchedulerPaths(
                        platform="macos",
                        launchd_plist=(
                            case_user_home
                            / "Library"
                            / "LaunchAgents"
                            / f"{MODULE.LAUNCHD_LABEL}.plist"
                        ),
                    )
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    legacy.parent.mkdir(parents=True)
                    parent_identity = (
                        legacy.parent.stat().st_dev,
                        legacy.parent.stat().st_ino,
                    )
                    real_directory_identity = MODULE._directory_identity
                    native_calls = 0
                    force_unreadable = False

                    def mutate_absence(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal force_unreadable, native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        if mutation == "appearance":
                            legacy.write_bytes(b"new user scheduler config\n")
                            legacy.chmod(0o600)
                        elif mutation == "parent":
                            displaced = legacy.parent.with_name(
                                legacy.parent.name + ".displaced"
                            )
                            legacy.parent.rename(displaced)
                            legacy.parent.mkdir()
                        else:
                            force_unreadable = True

                    def directory_identity(
                        directory_fd: int,
                    ) -> tuple[int, int]:
                        identity = real_directory_identity(directory_fd)
                        if force_unreadable and identity == parent_identity:
                            raise OSError("injected parent read failure")
                        return identity

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_absence,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_directory_identity",
                            side_effect=directory_identity,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE._cleanup_legacy_launchd_schedulers(
                            paths,
                            dry_run=False,
                            disable=True,
                            remove=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    if mutation == "appearance":
                        self.assertEqual(
                            legacy.read_bytes(),
                            b"new user scheduler config\n",
                        )

    def test_uninstall_prebinds_legacy_before_current_launchd_actions(self) -> None:
        cases = (
            ("appearance", "appeared after initial absence"),
            ("replacement", "object identity changed"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        case_index = 0
        for action_index in range(2):
            for mutation, expected_error in cases:
                case_index += 1
                with self.subTest(action=action_index, mutation=mutation):
                    case_user_home = self.root / f"legacy-current-{case_index}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                "macos",
                                None,
                                dry_run=False,
                                enable=False,
                            )
                        paths = MODULE._scheduler_paths("macos", case_home)
                    assert paths.launchd_plist is not None
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    if mutation == "replacement":
                        legacy.write_bytes(b"original legacy config\n")
                        legacy.chmod(0o600)
                    replacement_payload = (
                        f"new user config {action_index} {mutation}\n".encode("utf-8")
                    )
                    native_calls = 0

                    def mutate_legacy_during_current_action(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        if mutation == "appearance":
                            legacy.write_bytes(replacement_payload)
                            legacy.chmod(0o600)
                            return
                        candidate = legacy.with_name(legacy.name + ".replacement")
                        candidate.write_bytes(replacement_payload)
                        candidate.chmod(0o600)
                        os.replace(candidate, legacy)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_legacy_during_current_action,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            "macos",
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    self.assertEqual(legacy.read_bytes(), replacement_payload)
                    self.assertTrue(paths.launchd_plist.exists())
                    self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_revalidates_legacy_after_current_removal(
        self,
    ) -> None:
        for initial_legacy_exists in (False, True):
            with self.subTest(initial_legacy_exists=initial_legacy_exists):
                suffix = "present" if initial_legacy_exists else "absent"
                case_user_home = self.root / f"legacy-current-removal-{suffix}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths("macos", case_home)
                assert paths.launchd_plist is not None
                legacy = MODULE._legacy_launchd_plist(
                    paths,
                    MODULE.LEGACY_LAUNCHD_LABELS[0],
                )
                if initial_legacy_exists:
                    legacy.write_bytes(b"original legacy config\n")
                    legacy.chmod(0o600)
                payload = (
                    f"concurrent {suffix} legacy config during current removal\n"
                ).encode("utf-8")
                isolate = MODULE._isolate_and_delete_pending_cleanup_file

                def appear_during_current_removal(
                    home: Path,
                    path: Path,
                    parent_fd: int,
                    expected: MODULE.ManagedStateFileSnapshot,
                    *,
                    label: str,
                ) -> None:
                    isolate(
                        home,
                        path,
                        parent_fd,
                        expected,
                        label=label,
                    )
                    if path != paths.launchd_plist:
                        self.assertEqual(path, legacy)
                        return
                    legacy.write_bytes(payload)
                    legacy.chmod(0o600)

                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                        side_effect=appear_during_current_removal,
                    ),
                    contextlib.redirect_stdout(output),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        (
                            "reappeared after conditional removal"
                            if initial_legacy_exists
                            else "appeared after initial absence"
                        ),
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        "macos",
                        dry_run=False,
                        disable=False,
                    )

                self.assertFalse(paths.launchd_plist.exists())
                self.assertEqual(legacy.read_bytes(), payload)
                self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_missing_config_parent_no_disable_is_idempotent_noop(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"missing-parent-{platform_name}-no-disable" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "missing-parent no-op acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "missing-parent no-op ran a native command"
                        ),
                    ) as native_command,
                    contextlib.redirect_stdout(output),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=False,
                    )

                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                self.assertFalse(MODULE._scheduler_config_parent(paths).exists())
                self.assertFalse(case_home.exists())
                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertIn("scheduler already absent", output.getvalue())

    def test_uninstall_missing_config_disables_orphan_daemon_by_identity(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            for precreate_parent in (False, True):
                with self.subTest(
                    platform=platform_name,
                    precreate_parent=precreate_parent,
                ):
                    case_user_home = (
                        self.root
                        / f"orphan-{platform_name}-{precreate_parent}"
                        / "home"
                    )
                    case_user_home.mkdir(parents=True)
                    case_home = case_user_home / ".codex"
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    if precreate_parent:
                        MODULE._scheduler_config_parent(paths).mkdir(parents=True)
                    native_calls: list[list[str]] = []

                    def capture_native(
                        args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool | str = False,
                    ) -> None:
                        self.assertFalse(dry_run)
                        del allow_fail
                        native_calls.append(args)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_scheduler_daemon_enabled",
                            side_effect=(
                                MODULE.SchedulerDaemonQuery("enabled"),
                                MODULE.SchedulerDaemonQuery(
                                    "disabled",
                                    "daemon is absent",
                                ),
                            ),
                        ) as daemon_query,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=capture_native,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(daemon_query.call_count, 2)
                    self.assertTrue(MODULE._scheduler_config_parent(paths).is_dir())
                    self.assertFalse(
                        MODULE._scheduler_uninstall_transaction_path(paths).exists()
                    )
                    if platform_name == "macos":
                        domain = f"gui/{os.getuid()}"
                        self.assertIn(
                            [
                                "launchctl",
                                "bootout",
                                f"{domain}/{MODULE.LAUNCHD_LABEL}",
                            ],
                            native_calls,
                        )
                        for label in MODULE.LEGACY_LAUNCHD_LABELS:
                            self.assertIn(
                                [
                                    "launchctl",
                                    "bootout",
                                    f"{domain}/{label}",
                                ],
                                native_calls,
                            )
                        assert paths.launchd_plist is not None
                        self.assertFalse(paths.launchd_plist.exists())
                    else:
                        self.assertIn(
                            [
                                "systemctl",
                                "--user",
                                "disable",
                                "--now",
                                f"{MODULE.SYSTEMD_UNIT}.timer",
                            ],
                            native_calls,
                        )
                        self.assertIn(
                            ["systemctl", "--user", "daemon-reload"],
                            native_calls,
                        )
                        assert paths.systemd_service is not None
                        assert paths.systemd_timer is not None
                        self.assertFalse(paths.systemd_service.exists())
                        self.assertFalse(paths.systemd_timer.exists())

    def test_uninstall_orphan_daemon_uncertainty_and_failures_retain_marker(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            for failure_point in ("prequery", "native", "postquery"):
                with self.subTest(
                    platform=platform_name,
                    failure_point=failure_point,
                ):
                    case_user_home = (
                        self.root
                        / f"orphan-failure-{platform_name}-{failure_point}"
                        / "home"
                    )
                    case_user_home.mkdir(parents=True)
                    case_home = case_user_home / ".codex"
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    queries = (
                        (
                            MODULE.SchedulerDaemonQuery(
                                "unavailable",
                                "daemon query denied",
                            ),
                        )
                        if failure_point == "prequery"
                        else (
                            MODULE.SchedulerDaemonQuery("enabled"),
                            MODULE.SchedulerDaemonQuery("active-disabled"),
                        )
                    )
                    native_failure = (
                        MODULE.SyncError("native cleanup failed")
                        if failure_point == "native"
                        else None
                    )
                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_scheduler_daemon_enabled",
                            side_effect=queries,
                        ) as daemon_query,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=native_failure,
                        ) as native_command,
                        contextlib.redirect_stdout(output),
                        self.assertRaises(MODULE.SyncError) as raised,
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                    marker = MODULE._scheduler_uninstall_transaction_path(paths)
                    self.assertTrue(marker.is_file())
                    self.assertNotIn("removed ", output.getvalue())
                    if failure_point == "prequery":
                        self.assertEqual(
                            raised.exception.code,
                            "scheduler-uninstall-incomplete",
                        )
                        native_command.assert_not_called()
                    elif failure_point == "native":
                        self.assertEqual(str(raised.exception), "native cleanup failed")
                        self.assertEqual(daemon_query.call_count, 1)
                    else:
                        self.assertEqual(
                            raised.exception.code,
                            "scheduler-uninstall-incomplete",
                        )
                        self.assertEqual(daemon_query.call_count, 2)

    def test_uninstall_orphan_publishes_marker_before_native_cleanup(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-marker-failure-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_retain_scheduler_uninstall_transaction",
                        side_effect=MODULE.SyncError(
                            "marker publication failed",
                            code="scheduler-uninstall-incomplete",
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        side_effect=AssertionError(
                            "daemon query ran before durable marker publication"
                        ),
                    ) as daemon_query,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "native cleanup ran before durable marker publication"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "marker publication failed",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                daemon_query.assert_not_called()
                native_command.assert_not_called()
                self.assertTrue(MODULE._scheduler_config_parent(paths).is_dir())
                self.assertFalse(
                    MODULE._scheduler_uninstall_transaction_path(paths).exists()
                )

    def test_uninstall_orphan_retries_from_durable_marker(self) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = self.root / f"orphan-retry-{platform_name}" / "home"
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                marker = MODULE._scheduler_uninstall_transaction_path(paths)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        return_value=MODULE.SchedulerDaemonQuery(
                            "unavailable",
                            "daemon query denied",
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "native cleanup ran after an inconclusive pre-query"
                        ),
                    ) as first_native,
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(MODULE.SyncError) as first_failure,
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertEqual(
                    first_failure.exception.code,
                    "scheduler-uninstall-incomplete",
                )
                first_native.assert_not_called()
                self.assertTrue(marker.is_file())
                self.assertTrue(MODULE._scheduler_config_parent(paths).is_dir())

                native_calls: list[list[str]] = []

                def capture_native(
                    args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool | str = False,
                ) -> None:
                    self.assertFalse(dry_run)
                    del allow_fail
                    native_calls.append(args)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        side_effect=(
                            MODULE.SchedulerDaemonQuery("enabled"),
                            MODULE.SchedulerDaemonQuery("disabled"),
                        ),
                    ) as daemon_query,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=capture_native,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertEqual(daemon_query.call_count, 2)
                self.assertFalse(marker.exists())
                if platform_name == "macos":
                    domain = f"gui/{os.getuid()}"
                    self.assertIn(
                        [
                            "launchctl",
                            "bootout",
                            f"{domain}/{MODULE.LAUNCHD_LABEL}",
                        ],
                        native_calls,
                    )
                else:
                    self.assertIn(
                        [
                            "systemctl",
                            "--user",
                            "disable",
                            "--now",
                            f"{MODULE.SYSTEMD_UNIT}.timer",
                        ],
                        native_calls,
                    )

    def test_uninstall_orphan_binds_config_absence_across_daemon_query(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-config-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                payload = f"concurrent {platform_name} config\n".encode()
                native_queries: list[list[str]] = []

                def appear_during_query(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    native_queries.append(args)
                    target.write_bytes(payload)
                    target.chmod(0o600)
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "enabled\n",
                        "",
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=appear_during_query,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "native cleanup ran after scheduler config appeared"
                        ),
                    ) as native_command,
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "appeared after initial absence",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertEqual(len(native_queries), 1)
                native_command.assert_not_called()
                self.assertEqual(target.read_bytes(), payload)
                self.assertTrue(
                    MODULE._scheduler_uninstall_transaction_path(paths).is_file()
                )

    def test_uninstall_missing_parent_race_fails_closed_and_preserves_appearance(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"missing-parent-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                parent = MODULE._scheduler_config_parent(paths)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                payload = f"concurrent {platform_name} config\n".encode("utf-8")
                first_component = "Library" if platform_name == "macos" else ".config"
                real_open = MODULE.os.open
                injected = False

                def observe_missing_then_appear(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal injected
                    if (
                        not injected
                        and os.fspath(path) == first_component
                        and dir_fd is not None
                    ):
                        injected = True
                        parent.mkdir(parents=True)
                        target.write_bytes(payload)
                        target.chmod(0o600)
                        raise FileNotFoundError(first_component)
                    return real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=observe_missing_then_appear,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "missing-parent no-op acquired the install lock"
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "missing-parent no-op ran a native command"
                        ),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "appeared after a missing observation",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), payload)

    def test_uninstall_config_parent_uncertainty_fails_closed(self) -> None:
        for platform_name in ("macos", "linux"):
            for mutation, expected_error in (
                ("file", "is not a directory"),
                ("symlink", "is a symlink"),
                ("unreadable", "is unreadable"),
            ):
                with self.subTest(platform=platform_name, mutation=mutation):
                    case_user_home = (
                        self.root
                        / f"uncertain-parent-{platform_name}-{mutation}"
                        / "home"
                    )
                    case_home = case_user_home / ".codex"
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    parent = MODULE._scheduler_config_parent(paths)
                    parent.parent.mkdir(parents=True)
                    if mutation == "file":
                        parent.write_bytes(b"foreign parent object\n")
                    elif mutation == "symlink":
                        target = parent.with_name(parent.name + ".target")
                        target.mkdir()
                        parent.symlink_to(target, target_is_directory=True)
                    else:
                        parent.mkdir()

                    real_open = MODULE.os.open
                    real_stat = MODULE.os.stat

                    def reject_parent_open(
                        path: os.PathLike[str] | str,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        if (
                            mutation == "unreadable"
                            and os.fspath(path) == parent.name
                            and dir_fd is not None
                        ):
                            raise PermissionError(parent.name)
                        return real_open(
                            path,
                            flags,
                            mode,
                            dir_fd=dir_fd,
                        )

                    def reject_parent_read(
                        path: os.PathLike[str] | str,
                        *args: object,
                        **kwargs: object,
                    ) -> os.stat_result:
                        if (
                            mutation == "unreadable"
                            and os.fspath(path) == parent.name
                            and kwargs.get("dir_fd") is not None
                        ):
                            raise PermissionError(parent.name)
                        return real_stat(path, *args, **kwargs)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "open",
                            side_effect=reject_parent_open,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "stat",
                            side_effect=reject_parent_read,
                        ),
                        mock.patch.object(
                            MODULE,
                            "installation_lock",
                            side_effect=AssertionError(
                                "uncertain parent acquired the install lock"
                            ),
                        ) as install_lock,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=AssertionError(
                                "uncertain parent ran a native command"
                            ),
                        ) as native_command,
                        self.assertRaisesRegex(MODULE.SyncError, expected_error),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                    install_lock.assert_not_called()
                    native_command.assert_not_called()

    def test_uninstall_intermediate_config_parent_symlink_fails_closed(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"intermediate-symlink-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                if platform_name == "macos":
                    link = case_user_home / "Library"
                    link_target = case_user_home / "foreign-library"
                    config_parent = link_target / "LaunchAgents"
                    config_name = f"{MODULE.LAUNCHD_LABEL}.plist"
                else:
                    config_root = case_user_home / ".config"
                    config_root.mkdir()
                    link = config_root / "systemd"
                    link_target = case_user_home / "foreign-systemd"
                    config_parent = link_target / "user"
                    config_name = f"{MODULE.SYSTEMD_UNIT}.service"
                config_parent.mkdir(parents=True)
                link.symlink_to(link_target, target_is_directory=True)
                foreign_config = config_parent / config_name
                payload = f"foreign {platform_name} scheduler\n".encode("utf-8")
                foreign_config.write_bytes(payload)
                foreign_config.chmod(0o600)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "intermediate symlink acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "intermediate symlink ran a native command"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "component is a symlink",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertEqual(foreign_config.read_bytes(), payload)

    def test_uninstall_config_parent_component_replacement_fails_closed(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"component-replacement-{platform_name}" / "home"
                )
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                parent = MODULE._scheduler_config_parent(paths)
                parent.mkdir(parents=True)
                first_component = "Library" if platform_name == "macos" else ".config"
                original_component = case_user_home / first_component
                displaced_component = original_component.with_name(
                    original_component.name + ".displaced"
                )
                real_open = MODULE.os.open
                injected = False

                def replace_opened_component(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal injected
                    file_descriptor = real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )
                    if (
                        not injected
                        and os.fspath(path) == first_component
                        and dir_fd is not None
                    ):
                        injected = True
                        original_component.rename(displaced_component)
                        original_component.mkdir()
                    return file_descriptor

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=replace_opened_component,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "component replacement acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "component replacement ran a native command"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "component identity changed",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertTrue(injected)
                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertTrue(displaced_component.exists())

    def test_uninstall_binds_original_scheduler_configs_across_native_calls(
        self,
    ) -> None:
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("owner", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        case_index = 0
        for platform_name in ("macos", "linux"):
            targets = ("plist",) if platform_name == "macos" else ("service", "timer")
            action_indexes = range(2) if platform_name == "macos" else range(1)
            for target_kind in targets:
                for action_index in action_indexes:
                    for mutation, expected_error in mutations:
                        case_index += 1
                        with self.subTest(
                            platform=platform_name,
                            target=target_kind,
                            action=action_index,
                            mutation=mutation,
                        ):
                            case_user_home = (
                                self.root / f"uninstall-{case_index}" / "home"
                            )
                            case_home = case_user_home / ".codex"
                            runner = case_home / "bin" / "codex-personal-sync"
                            runner.parent.mkdir(parents=True)
                            runner.write_text(
                                "#!/bin/sh\nexit 0\n",
                                encoding="utf-8",
                            )
                            runner.chmod(0o755)
                            with mock.patch.object(
                                MODULE.Path,
                                "home",
                                return_value=case_user_home,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    MODULE.install_scheduler(
                                        case_home,
                                        "owner/public-sync",
                                        17,
                                        platform_name,
                                        None,
                                        dry_run=False,
                                        enable=False,
                                    )
                                paths = MODULE._scheduler_paths(
                                    platform_name,
                                    case_home,
                                )
                            if target_kind == "plist":
                                target = paths.launchd_plist
                            elif target_kind == "service":
                                target = paths.systemd_service
                            else:
                                target = paths.systemd_timer
                            assert target is not None
                            original = target.read_bytes()
                            original_metadata = target.stat()
                            original_identity = (
                                original_metadata.st_dev,
                                original_metadata.st_ino,
                            )
                            original_mode = stat.S_IMODE(original_metadata.st_mode)
                            real_read = MODULE._read_managed_state_bytes
                            real_fstat = MODULE.os.fstat
                            native_calls = 0
                            force_owner = False
                            force_unreadable = False

                            def mutate_during_uninstall(
                                _args: list[str],
                                *,
                                dry_run: bool,
                                allow_fail: bool = False,
                            ) -> None:
                                del dry_run, allow_fail
                                nonlocal force_owner
                                nonlocal force_unreadable
                                nonlocal native_calls
                                current_call = native_calls
                                native_calls += 1
                                if current_call != action_index:
                                    return
                                if mutation == "replacement":
                                    candidate = target.with_name(
                                        target.name + ".replacement"
                                    )
                                    candidate.write_bytes(original)
                                    candidate.chmod(original_mode)
                                    os.replace(candidate, target)
                                elif mutation == "content":
                                    payload = bytearray(original)
                                    payload[len(payload) // 2] ^= 1
                                    target.write_bytes(payload)
                                    target.chmod(original_mode)
                                elif mutation == "mode":
                                    target.chmod(
                                        0o644 if original_mode != 0o644 else 0o600
                                    )
                                elif mutation == "owner":
                                    force_owner = True
                                elif mutation == "parent":
                                    displaced = target.parent.with_name(
                                        target.parent.name + ".displaced"
                                    )
                                    target.parent.rename(displaced)
                                    target.parent.mkdir()
                                elif mutation == "missing":
                                    target.unlink()
                                else:
                                    force_unreadable = True

                            def fstat_with_owner(
                                file_fd: int,
                            ) -> os.stat_result:
                                metadata = real_fstat(file_fd)
                                if (
                                    force_owner
                                    and (metadata.st_dev, metadata.st_ino)
                                    == original_identity
                                ):
                                    fields = list(metadata)
                                    fields[4] = metadata.st_uid + 1
                                    return os.stat_result(fields)
                                return metadata

                            def fail_bound_read(
                                file_fd: int,
                                path: Path,
                                maximum_bytes: int = (MODULE.MAX_MANAGED_STATE_BYTES),
                            ) -> bytes:
                                if force_unreadable and path == target:
                                    raise MODULE.SyncError(
                                        "injected bound read failure"
                                    )
                                return real_read(
                                    file_fd,
                                    path,
                                    maximum_bytes,
                                )

                            output = io.StringIO()
                            with (
                                mock.patch.object(
                                    MODULE.Path,
                                    "home",
                                    return_value=case_user_home,
                                ),
                                mock.patch.object(
                                    MODULE,
                                    "_run_native_command",
                                    side_effect=mutate_during_uninstall,
                                ),
                                mock.patch.object(
                                    MODULE.os,
                                    "fstat",
                                    side_effect=fstat_with_owner,
                                ),
                                mock.patch.object(
                                    MODULE,
                                    "_read_managed_state_bytes",
                                    side_effect=fail_bound_read,
                                ),
                                contextlib.redirect_stdout(output),
                                self.assertRaisesRegex(
                                    MODULE.SyncError,
                                    expected_error,
                                ),
                            ):
                                MODULE.uninstall_scheduler(
                                    case_home,
                                    platform_name,
                                    dry_run=False,
                                    disable=True,
                                )

                            self.assertEqual(
                                native_calls,
                                action_index + 1,
                            )
                            self.assertNotIn(
                                "removed ",
                                output.getvalue(),
                            )
                            if mutation == "replacement":
                                self.assertEqual(target.read_bytes(), original)

    def test_linux_uninstall_preserves_foreign_systemd_drop_ins(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
            enable=False,
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        service_drop_in = paths.systemd_service.with_name(
            paths.systemd_service.name + ".d"
        )
        timer_drop_in = paths.systemd_timer.with_name(paths.systemd_timer.name + ".d")
        service_drop_in.mkdir()
        timer_drop_in.mkdir()
        service_override = service_drop_in / "foreign.conf"
        timer_override = timer_drop_in / "foreign.conf"
        service_override.write_text("[Service]\nNice=5\n", encoding="utf-8")
        timer_override.write_text("[Timer]\nRandomizedDelaySec=1m\n", encoding="utf-8")

        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: ["/usr/bin/systemctl", *args[1:]],
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )

        self.assertFalse(paths.systemd_service.exists())
        self.assertFalse(paths.systemd_timer.exists())
        self.assertEqual(
            service_override.read_text(encoding="utf-8"), "[Service]\nNice=5\n"
        )
        self.assertEqual(
            timer_override.read_text(encoding="utf-8"),
            "[Timer]\nRandomizedDelaySec=1m\n",
        )

    def test_linux_uninstall_revalidates_unit_absence_around_daemon_reload(
        self,
    ) -> None:
        case_index = 0
        for target_kind in ("service", "timer"):
            for appearance_phase in ("before", "during"):
                case_index += 1
                with self.subTest(
                    target=target_kind,
                    phase=appearance_phase,
                ):
                    case_user_home = self.root / f"reload-absence-{case_index}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                "linux",
                                None,
                                dry_run=False,
                                enable=False,
                            )
                        paths = MODULE._scheduler_paths("linux", case_home)
                    target = (
                        paths.systemd_service
                        if target_kind == "service"
                        else paths.systemd_timer
                    )
                    other = (
                        paths.systemd_timer
                        if target_kind == "service"
                        else paths.systemd_service
                    )
                    assert target is not None
                    assert other is not None
                    payload = (f"reappeared {target_kind} {appearance_phase}\n").encode(
                        "utf-8"
                    )
                    native_calls = 0
                    real_report = MODULE._report_preserved_systemd_drop_ins

                    def appear_before_reload(
                        selected_paths: MODULE.SchedulerPaths,
                    ) -> None:
                        real_report(selected_paths)
                        if appearance_phase == "before":
                            target.write_bytes(payload)
                            target.chmod(0o600)

                    def appear_during_reload(
                        args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        native_calls += 1
                        if appearance_phase == "during" and args[-1] == "daemon-reload":
                            target.write_bytes(payload)
                            target.chmod(0o600)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_report_preserved_systemd_drop_ins",
                            side_effect=appear_before_reload,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=appear_during_reload,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "reappeared after conditional removal",
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            "linux",
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(
                        native_calls,
                        1 if appearance_phase == "before" else 2,
                    )
                    self.assertEqual(target.read_bytes(), payload)
                    self.assertFalse(other.exists())
                    self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_commit_revalidates_shared_parent_group(self) -> None:
        for platform_name in ("macos", "linux"):
            for mutation in ("during-fsync", "during-second-pass"):
                with self.subTest(
                    platform=platform_name,
                    mutation=mutation,
                ):
                    case_user_home = (
                        self.root / f"commit-group-{platform_name}-{mutation}" / "home"
                    )
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                platform_name,
                                None,
                                dry_run=False,
                                enable=False,
                            )
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(
                            platform_name,
                            case_home,
                        )
                    target = (
                        paths.launchd_plist
                        if platform_name == "macos"
                        else paths.systemd_service
                    )
                    assert target is not None
                    marker = MODULE._scheduler_uninstall_transaction_path(paths)
                    original = target.read_bytes()
                    real_commit = MODULE._commit_scheduler_uninstall_transaction
                    real_fsync = MODULE.os.fsync
                    real_revalidate = MODULE._revalidate_launchd_activation_binding
                    commit_active = False
                    injected = False
                    marker_checks = 0
                    commit_parent_fds: list[int] = []

                    def reappear_target() -> None:
                        nonlocal injected
                        target.write_bytes(original)
                        target.chmod(0o600)
                        injected = True

                    def commit_and_arm(
                        marker_binding: MODULE.SchedulerActivationBinding,
                        *,
                        related_bindings: tuple[
                            MODULE.SchedulerActivationBinding,
                            ...,
                        ],
                    ) -> None:
                        nonlocal commit_active
                        commit_active = True
                        real_commit(
                            marker_binding,
                            related_bindings=related_bindings,
                        )

                    def sync_then_reappear(file_fd: int) -> None:
                        real_fsync(file_fd)
                        if (
                            commit_active
                            and mutation == "during-fsync"
                            and not injected
                        ):
                            reappear_target()

                    def revalidate_and_interleave(
                        binding: MODULE.SchedulerActivationBinding,
                        *,
                        boundary: str,
                    ) -> None:
                        nonlocal marker_checks
                        if commit_active and boundary == (
                            "after parent sync before uninstall transaction commit"
                        ):
                            commit_parent_fds.append(binding.parent_fd)
                            if (
                                mutation == "during-second-pass"
                                and binding.path == marker
                            ):
                                marker_checks += 1
                                if marker_checks == 2:
                                    reappear_target()
                        real_revalidate(binding, boundary=boundary)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_commit_scheduler_uninstall_transaction",
                            side_effect=commit_and_arm,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "fsync",
                            side_effect=sync_then_reappear,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_revalidate_launchd_activation_binding",
                            side_effect=revalidate_and_interleave,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "reappeared after conditional removal",
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=False,
                        )

                    self.assertTrue(injected)
                    self.assertTrue(marker.is_file())
                    self.assertTrue(target.is_file())
                    self.assertTrue(commit_parent_fds)
                    self.assertEqual(len(set(commit_parent_fds)), 1)

    def test_uninstall_native_failures_retain_transaction_and_configs(
        self,
    ) -> None:
        success = subprocess.CompletedProcess(
            ["scheduler-action"],
            0,
            "",
            "",
        )
        failures: tuple[
            tuple[str, str, int, BaseException | subprocess.CompletedProcess[str]],
            ...,
        ] = (
            (
                "macos",
                "timeout",
                0,
                MODULE.SyncError(
                    "scheduler native command exceeded its monotonic deadline",
                    code="scheduler-timeout",
                ),
            ),
            (
                "macos",
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["launchctl", "bootout"],
                    1,
                    "",
                    "Operation not permitted",
                ),
            ),
            (
                "macos",
                "unknown",
                1,
                subprocess.CompletedProcess(
                    ["launchctl", "disable"],
                    1,
                    "",
                    "Input/output error",
                ),
            ),
            (
                "linux",
                "timeout",
                0,
                MODULE.SyncError(
                    "scheduler native command exceeded its monotonic deadline",
                    code="scheduler-timeout",
                ),
            ),
            (
                "linux",
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["systemctl", "disable"],
                    1,
                    "",
                    "Permission denied",
                ),
            ),
            (
                "linux",
                "unknown",
                0,
                subprocess.CompletedProcess(
                    ["systemctl", "disable"],
                    1,
                    "",
                    "Unit operation failed",
                ),
            ),
        )
        for platform_name, failure_kind, failure_call, failure in failures:
            with self.subTest(
                platform=platform_name,
                failure=failure_kind,
            ):
                case_user_home = (
                    self.root / f"native-{platform_name}-{failure_kind}" / "home"
                )
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(
                        platform_name,
                        case_home,
                    )
                    native_results: list[
                        BaseException | subprocess.CompletedProcess[str]
                    ] = [success] * failure_call + [failure]
                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=native_results,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaises(MODULE.SyncError),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                config_paths = (
                    (paths.launchd_plist,)
                    if platform_name == "macos"
                    else (paths.systemd_service, paths.systemd_timer)
                )
                self.assertTrue(
                    all(path is not None and path.is_file() for path in config_paths)
                )
                self.assertTrue(
                    MODULE._scheduler_uninstall_transaction_path(paths).is_file()
                )
                self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_accepts_only_precise_absence_evidence(self) -> None:
        uid = os.getuid()
        legacy_label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        accepted = (
            (
                [
                    "launchctl",
                    "bootout",
                    "gui/501",
                    "/tmp/scheduler.plist",
                ],
                "Boot-out failed: 3: No such process",
            ),
            (
                ["launchctl", "disable", "gui/501/example"],
                "Could not find specified service",
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                ["launchctl", "bootout", f"gui/{uid}/{legacy_label}"],
                (
                    "Bad request.\n"
                    f'Could not find service "{legacy_label}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "bootout",
                    f"gui/{uid}",
                    f"/tmp/{legacy_label}.plist",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{legacy_label}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (
                    "Failed to disable unit: Unit "
                    f"{MODULE.SYSTEMD_UNIT}.timer not loaded."
                ),
            ),
        )
        for args, stderr in accepted:
            with self.subTest(args=args):
                completed = subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    stderr,
                )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda selected: selected,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        return_value=completed,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE._run_native_command(
                        args,
                        dry_run=False,
                        allow_fail=MODULE.NATIVE_FAILURE_ALREADY_ABSENT,
                    )

        rejected = (
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (
                    "Failed to disable unit: Unit file "
                    f"{MODULE.SYSTEMD_UNIT}.timer does not exist."
                ),
            ),
            (
                ["launchctl", "disable", "gui/501/example"],
                "Permission denied: Could not find specified service",
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    'Could not find service "mismatched.label" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL.upper()}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid + 1}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid + 1}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid}"
                ),
            ),
            (
                [
                    "launchctl",
                    "disable",
                    f"gui/{uid}/{MODULE.LAUNCHD_LABEL}",
                ],
                (
                    "Bad request.\n"
                    f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                    f"in domain for user gui: {uid}\n"
                    "additional diagnostic"
                ),
            ),
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (f"Permission denied: Unit {MODULE.SYSTEMD_UNIT}.timer not loaded."),
            ),
        )
        for args, stderr in rejected:
            with self.subTest(stderr=stderr):
                completed = subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    stderr,
                )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda selected: selected,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        return_value=completed,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        re.escape(stderr),
                    ),
                ):
                    MODULE._run_native_command(
                        args,
                        dry_run=False,
                        allow_fail=MODULE.NATIVE_FAILURE_ALREADY_ABSENT,
                    )

    def test_linux_uninstall_reload_failure_is_reported_and_recoverable(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        marker = MODULE._scheduler_uninstall_transaction_path(paths)
        failed_results = (
            subprocess.CompletedProcess(
                ["systemctl", "disable"],
                0,
                "",
                "",
            ),
            subprocess.CompletedProcess(
                ["systemctl", "daemon-reload"],
                1,
                "",
                "Permission denied",
            ),
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=failed_results,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(MODULE.SyncError, "Permission denied"),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )

        self.assertFalse(paths.systemd_service.exists())
        self.assertFalse(paths.systemd_timer.exists())
        self.assertTrue(marker.is_file())
        self.assertNotIn("removed ", output.getvalue())
        with (
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
            mock.patch.object(
                MODULE,
                "audit_active_skills",
                return_value=[],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=False,
            )
        self.assertEqual(
            report.failure_code,
            "scheduler-uninstall-incomplete",
        )
        uninstall_issues = [
            issue for issue in issues if issue.code == "scheduler-uninstall-incomplete"
        ]
        self.assertEqual(len(uninstall_issues), 1)
        self.assertEqual(uninstall_issues[0].path, marker)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "incomplete uninstall transaction",
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

        recovered_results = (
            subprocess.CompletedProcess(
                ["systemctl", "disable"],
                1,
                "",
                (
                    "Failed to disable unit: Unit "
                    f"{MODULE.SYSTEMD_UNIT}.timer not loaded."
                ),
            ),
            subprocess.CompletedProcess(
                ["systemctl", "daemon-reload"],
                0,
                "",
                "",
            ),
        )
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                side_effect=(
                    MODULE.SchedulerDaemonQuery("enabled"),
                    MODULE.SchedulerDaemonQuery("disabled"),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=recovered_results,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )
        self.assertFalse(marker.exists())

    def test_bounded_scheduler_process_enforces_raw_byte_limits(self) -> None:
        limit = 4096
        with (
            mock.patch.object(MODULE, "MAX_SCHEDULER_NATIVE_STDOUT_BYTES", limit),
            mock.patch.object(MODULE, "MAX_SCHEDULER_NATIVE_STDERR_BYTES", limit),
        ):
            exact = MODULE._run_bounded_scheduler_process(
                [
                    sys.executable,
                    "-c",
                    (f"import os;os.write(1,b'o'*{limit});os.write(2,b'e'*{limit})"),
                ],
                timeout_seconds=5.0,
            )
            self.assertEqual(len(exact.stdout.encode("utf-8")), limit)
            self.assertEqual(len(exact.stderr.encode("utf-8")), limit)

            for stream_name, file_descriptor in (("stdout", 1), ("stderr", 2)):
                with self.subTest(stream=stream_name):
                    processes: list[subprocess.Popen[bytes]] = []
                    real_popen = subprocess.Popen

                    def capture_process(args, **kwargs):
                        process = real_popen(args, **kwargs)
                        processes.append(process)
                        return process

                    with (
                        mock.patch.object(
                            MODULE.subprocess,
                            "Popen",
                            side_effect=capture_process,
                        ),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            f"{stream_name} exceeds the {limit}-byte raw output limit",
                        ) as raised,
                    ):
                        MODULE._run_bounded_scheduler_process(
                            [
                                sys.executable,
                                "-c",
                                (
                                    "import os,time;"
                                    f"os.write({file_descriptor},b'x'*{limit + 1});"
                                    "time.sleep(30)"
                                ),
                            ],
                            timeout_seconds=5.0,
                        )

                    self.assertEqual(raised.exception.code, "scheduler-output-limit")
                    self.assertEqual(len(processes), 1)
                    self.assertIsNotNone(processes[0].poll())

    def test_bounded_scheduler_process_times_out_and_reaps_child(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def capture_process(args, **kwargs):
            process = real_popen(args, **kwargs)
            processes.append(process)
            return process

        with (
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_process,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exceeded its monotonic deadline",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=0.05,
            )

        self.assertEqual(raised.exception.code, "scheduler-timeout")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_scheduler_guardian_fences_descendant_after_target_exit(self) -> None:
        fifo = self.root / "guardian-liveness.fifo"
        os.mkfifo(fifo, 0o600)
        read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            completed = MODULE._run_bounded_scheduler_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,subprocess,sys;"
                        "fd=os.open(sys.argv[1],os.O_WRONLY);"
                        "os.write(fd,b'R');"
                        "subprocess.Popen("
                        "[sys.executable,'-c','import time;time.sleep(30)'],"
                        "stdin=subprocess.DEVNULL,"
                        "stdout=subprocess.DEVNULL,"
                        "stderr=subprocess.DEVNULL,"
                        "pass_fds=(fd,));"
                        "os.close(fd)"
                    ),
                    str(fifo),
                ],
                timeout_seconds=5.0,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(os.read(read_fd, 1), b"R")
            self.assertEqual(os.read(read_fd, 1), b"")
        finally:
            os.close(read_fd)

    def test_bounded_scheduler_selector_close_failure_preserves_primary(
        self,
    ) -> None:
        process = MODULE._spawn_guarded_process(
            [sys.executable, "-c", "import os;os.write(1,b'123456789')"],
            deadline=time.monotonic() + 5.0,
            process_label="test scheduler",
            unavailable_code="test-unavailable",
            unavailable_message="test executable unavailable",
        )
        real_selector = MODULE.selectors.DefaultSelector
        selector_calls = 0

        class CloseFailingSelector:
            def __init__(self, inner) -> None:
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def close(self) -> None:
                self.inner.close()
                raise ValueError("injected scheduler selector close failure")

        def selector_factory():
            nonlocal selector_calls
            selector_calls += 1
            if selector_calls == 1:
                return CloseFailingSelector(real_selector())
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
            mock.patch.object(MODULE, "MAX_SCHEDULER_NATIVE_STDOUT_BYTES", 8),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "stdout exceeds.*selector close failure",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "unused"],
                timeout_seconds=5.0,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertIsInstance(raised.exception.__cause__, MODULE.SyncError)
        self.assertEqual(
            raised.exception.__cause__.code,
            "scheduler-output-limit",
        )
        self.assertEqual(process.returncode, -MODULE.signal.SIGKILL)

    def test_scheduler_spawn_cleanup_failure_cannot_be_ignored(self) -> None:
        primary = MODULE.SyncError(
            "injected guardian cleanup failure",
            code="process-guardian-cleanup-inconclusive",
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                return_value=[sys.executable, "-c", "pass"],
            ),
            mock.patch.object(
                MODULE,
                "_spawn_guarded_process",
                side_effect=primary,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "guardian cleanup was inconclusive",
            ) as raised,
        ):
            MODULE._run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=False,
                allow_fail=True,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertNotIn("ignored failed command", output.getvalue())

    def test_scheduler_maps_guardian_operation_deadline_to_lane_taxonomy(
        self,
    ) -> None:
        primary = MODULE.SyncError(
            "scheduler native command exceeded its monotonic deadline",
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
                "scheduler native command exceeded its monotonic deadline",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "pass"],
                timeout_seconds=1.0,
            )

        self.assertEqual(raised.exception.code, "scheduler-timeout")
        self.assertIs(raised.exception.__cause__, primary)

    def test_bounded_scheduler_process_reaps_child_when_selectors_fail(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen
        real_selector = MODULE.selectors.DefaultSelector
        selector_calls = 0

        def capture_process(args, **kwargs):
            process = real_popen(args, **kwargs)
            processes.append(process)
            return process

        def fail_after_ready_selector():
            nonlocal selector_calls
            selector_calls += 1
            if selector_calls == 1:
                return real_selector()
            raise OSError("simulated selector exhaustion")

        with (
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_process,
            ),
            mock.patch.object(
                MODULE.selectors,
                "DefaultSelector",
                side_effect=fail_after_ready_selector,
            ),
            mock.patch.object(MODULE, "GH_CLEANUP_TIMEOUT_SECONDS", 1.0),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "cleanup was inconclusive.*cleanup selector",
            ) as raised,
        ):
            MODULE._run_bounded_scheduler_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=5.0,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, -9)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)

    def test_scheduler_cleanup_uncertainty_preserves_primary_classification(
        self,
    ) -> None:
        primary = MODULE.SyncError(
            "scheduler stdout exceeded its raw output limit",
            code="scheduler-output-limit",
        )
        incomplete = MODULE._GhCleanupReceipt(
            kill_sent=True,
            child_reaped=False,
            stdout_drained=False,
            stderr_drained=True,
            status_drained=False,
            process_group_fenced=False,
            errors=("simulated cleanup uncertainty",),
        )
        with (
            mock.patch.object(
                MODULE,
                "_cleanup_gh_process_group",
                return_value=incomplete,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "scheduler stdout exceeded.*cleanup was inconclusive",
            ) as raised,
        ):
            MODULE._raise_scheduler_failure_after_cleanup(mock.Mock(), primary)

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertIs(raised.exception.__cause__, primary)
        self.assertIn("stdout-not-drained", str(raised.exception))

    def test_scheduler_daemon_query_reports_stream_limit_without_payload(
        self,
    ) -> None:
        payload = "unbounded-native-payload"
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=MODULE.SyncError(
                    f"scheduler stdout exceeded; suppressed {payload}",
                    code="scheduler-output-limit",
                ),
            ),
        ):
            query = MODULE._scheduler_daemon_enabled(
                MODULE.SchedulerPaths(platform="macos")
            )

        self.assertEqual(query.classification, "unavailable")
        self.assertIn("output exceeded its byte limit", query.reason or "")
        self.assertNotIn(payload, query.reason or "")

    def test_scheduler_native_allow_fail_does_not_echo_overflow_payload(self) -> None:
        payload = "unbounded-native-payload"
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=MODULE.SyncError(
                    "scheduler stdout exceeded its raw output limit",
                    code="scheduler-output-limit",
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            MODULE._run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=False,
                allow_fail=True,
            )

        self.assertNotIn(payload, output.getvalue())
        self.assertIn("raw output limit", output.getvalue())

    def test_scheduler_native_allow_fail_propagates_cleanup_uncertainty(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=MODULE.SyncError(
                    "scheduler cleanup could not be proved complete",
                    code="scheduler-cleanup-inconclusive",
                ),
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "scheduler cleanup could not be proved complete",
            ) as raised,
        ):
            MODULE._run_native_command(
                ["launchctl", "bootout", "gui/501/com.example.sync"],
                dry_run=False,
                allow_fail=True,
            )

        self.assertEqual(raised.exception.code, "scheduler-cleanup-inconclusive")
        self.assertEqual(output.getvalue(), "")

    def test_scheduler_daemon_query_classifies_only_explicit_state_evidence(
        self,
    ) -> None:
        cases = (
            ("macos", 0, "", "", None, None, None, "enabled", None),
            (
                "macos",
                113,
                "",
                "Could not find service in domain",
                None,
                None,
                None,
                "disabled",
                "not loaded",
            ),
            (
                "macos",
                1,
                "",
                "Operation not permitted",
                None,
                None,
                None,
                "unavailable",
                "denied",
            ),
            (
                "linux",
                0,
                "enabled\n",
                "",
                0,
                "active\n",
                "",
                "enabled",
                None,
            ),
            (
                "linux",
                0,
                "enabled\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "enabled but not active",
            ),
            (
                "linux",
                0,
                "enabled-runtime\n",
                "",
                0,
                "active\n",
                "",
                "active-disabled",
                "runtime",
            ),
            (
                "linux",
                0,
                "enabled-runtime\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "non-terminal unit state enabled-runtime",
            ),
            (
                "linux",
                1,
                "linked-runtime\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "non-terminal unit state linked-runtime",
            ),
            (
                "linux",
                1,
                "static\n",
                "",
                3,
                "inactive\n",
                "",
                "enabled-inactive",
                "non-terminal unit state static",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "",
                0,
                "active\n",
                "",
                "active-disabled",
                "activity state active",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "",
                0,
                "inactive\n",
                "",
                "unavailable",
                "contradictory",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "",
                3,
                "inactive\n",
                "",
                "disabled",
                "state disabled",
            ),
            (
                "linux",
                1,
                "",
                "Failed to connect to bus",
                3,
                "inactive\n",
                "",
                "unavailable",
                "user bus",
            ),
            (
                "linux",
                1,
                "unexpected\n",
                "",
                3,
                "inactive\n",
                "",
                "unavailable",
                "no recognized",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "Permission denied",
                3,
                "inactive\n",
                "",
                "unavailable",
                "denied",
            ),
        )
        for (
            platform_name,
            enable_returncode,
            enable_stdout,
            enable_stderr,
            active_returncode,
            active_stdout,
            active_stderr,
            classification,
            reason,
        ) in cases:
            with self.subTest(
                platform=platform_name,
                enable_returncode=enable_returncode,
                enable_stdout=enable_stdout,
                enable_stderr=enable_stderr,
                active_returncode=active_returncode,
                active_stdout=active_stdout,
                active_stderr=active_stderr,
            ):
                completed = subprocess.CompletedProcess(
                    ["scheduler-query"],
                    enable_returncode,
                    enable_stdout,
                    enable_stderr,
                )
                results = [completed]
                if platform_name == "linux":
                    results.append(
                        subprocess.CompletedProcess(
                            ["scheduler-activity-query"],
                            active_returncode,
                            active_stdout,
                            active_stderr,
                        )
                    )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=results,
                    ),
                ):
                    query = MODULE._scheduler_daemon_enabled(
                        MODULE.SchedulerPaths(platform=platform_name)
                    )
                self.assertEqual(query.classification, classification)
                self.assertEqual(
                    query.enabled,
                    (
                        True
                        if classification == "enabled"
                        else False
                        if classification
                        in {
                            "active-disabled",
                            "disabled",
                            "enabled-inactive",
                        }
                        else None
                    ),
                )
                if reason is not None:
                    self.assertIn(reason, query.reason or "")

    def test_macos_daemon_query_rejects_mixed_absence_and_denial(self) -> None:
        for evidence, expected_classification, expected_reason in (
            (
                "Permission denied: Could not find service in domain",
                "unavailable",
                "denied",
            ),
            (
                "Input/output error: Could not find service in domain",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                f"in domain for user gui: {os.getuid()}",
                "disabled",
                "not loaded",
            ),
            (
                "Bad request.\n"
                'Could not find service "mismatched.label" '
                f"in domain for user gui: {os.getuid()}",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL.upper()}" '
                f"in domain for user gui: {os.getuid()}",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                f"in domain for user gui: {os.getuid() + 1}",
                "unavailable",
                "without explicit",
            ),
            (
                "Bad request.\n"
                f'Could not find service "{MODULE.LAUNCHD_LABEL}" '
                f"in domain for user gui: {os.getuid()}\n"
                "additional diagnostic",
                "unavailable",
                "without explicit",
            ),
        ):
            with self.subTest(evidence=evidence):
                completed = subprocess.CompletedProcess(
                    ["launchctl", "print"],
                    113,
                    "",
                    evidence,
                )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        return_value=completed,
                    ),
                ):
                    query = MODULE._scheduler_daemon_enabled(
                        MODULE.SchedulerPaths(platform="macos")
                    )
                self.assertEqual(
                    query.classification,
                    expected_classification,
                )
                self.assertIn(expected_reason, query.reason or "")

    def test_scheduler_report_and_doctor_detect_orphan_daemon(self) -> None:
        for platform_name, classification in (
            ("macos", "enabled"),
            ("linux", "active-disabled"),
            ("linux", "enabled-inactive"),
        ):
            with self.subTest(
                platform=platform_name,
                classification=classification,
            ):
                case_user_home = (
                    self.root
                    / f"orphan-status-{platform_name}-{classification}"
                    / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                query = MODULE.SchedulerDaemonQuery(
                    classification,
                    f"observed {classification}",
                )
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_daemon_enabled",
                        return_value=query,
                    ) as daemon_query,
                    mock.patch.object(
                        MODULE,
                        "audit_active_skills",
                        return_value=[],
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    report = MODULE.scheduler_report(
                        case_home,
                        platform_name,
                    )
                    _doctor_report, issues = MODULE.doctor(
                        case_home,
                        platform_name,
                        json_output=False,
                    )

                self.assertGreaterEqual(daemon_query.call_count, 2)
                self.assertFalse(report.installed)
                self.assertTrue(report.enabled)
                self.assertEqual(report.failure_code, "scheduler-orphan-active")
                self.assertEqual(
                    report.daemon_query,
                    query,
                )
                issue_codes = {issue.code for issue in issues}
                self.assertIn("scheduler-not-installed", issue_codes)
                self.assertIn("scheduler-orphan-active", issue_codes)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                self.assertFalse(MODULE._scheduler_config_parent(paths).exists())
                self.assertFalse(case_home.exists())

    def test_scheduler_status_binds_missing_parent_activation_absence(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-activation-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                marker = MODULE._scheduler_activation_transaction_path(paths)

                def begin_activation_during_query(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_bytes(
                        MODULE._scheduler_activation_transaction_payload(
                            case_home,
                            paths,
                        )
                    )
                    marker.chmod(0o600)
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "enabled\n",
                        "",
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=begin_activation_during_query,
                    ),
                ):
                    report = MODULE.scheduler_report(
                        case_home,
                        platform_name,
                    )

                self.assertTrue(marker.is_file())
                self.assertFalse(report.installed)
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )

    def test_scheduler_status_binds_missing_parent_uninstall_absence(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"orphan-uninstall-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                marker = MODULE._scheduler_uninstall_transaction_path(paths)

                def begin_uninstall_during_query(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_bytes(
                        MODULE._scheduler_uninstall_transaction_payload(
                            case_home,
                            paths,
                            disable=True,
                        )
                    )
                    marker.chmod(0o600)
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "enabled\n",
                        "",
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=begin_uninstall_during_query,
                    ),
                ):
                    report = MODULE.scheduler_report(
                        case_home,
                        platform_name,
                    )

                self.assertTrue(marker.is_file())
                self.assertFalse(report.installed)
                self.assertIsNone(report.enabled)
                self.assertEqual(
                    report.failure_code,
                    "scheduler-uninstall-incomplete",
                )
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )

    def test_linux_status_requires_enabled_and_active_timer(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        query_results = (
            subprocess.CompletedProcess(
                ["systemctl", "is-enabled"],
                0,
                "enabled\n",
                "",
            ),
            subprocess.CompletedProcess(
                ["systemctl", "is-active"],
                3,
                "failed\n",
                "",
            ),
        )
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=query_results,
            ) as run,
            mock.patch.object(
                MODULE,
                "_stable_scheduler_runner_matches",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            [call.args[0][2] for call in run.call_args_list],
            ["is-enabled", "is-active"],
        )
        self.assertFalse(report.enabled)
        self.assertEqual(
            report.failure_code,
            "scheduler-daemon-disabled",
        )
        assert report.daemon_query is not None
        self.assertEqual(
            report.daemon_query.classification,
            "enabled-inactive",
        )
        self.assertIn(
            "enabled but not active (state failed)",
            report.daemon_query.reason or "",
        )

    def test_scheduler_report_and_doctor_preserve_runtime_and_daemon_failures(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        runtime_state = {
            "version": 2,
            "last_attempt": "2026-07-24T12:00:00+00:00",
            "last_success": None,
            "success": False,
            "failure_reason": "scheduled sync failed",
            "failure_code": None,
            "release_trees": {},
            "mode": "public",
            "repo": "owner/public-sync",
            "base_repo": "owner/public-sync",
            "owner": MODULE.PUBLIC_OWNER,
        }
        enablement = subprocess.CompletedProcess(
            ["systemctl"],
            1,
            "disabled\n",
            "",
        )
        activity = subprocess.CompletedProcess(
            ["systemctl"],
            3,
            "inactive\n",
            "",
        )
        with (
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
            mock.patch.object(
                MODULE,
                "_stable_scheduler_runner_matches",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE,
                "_run_bounded_scheduler_process",
                side_effect=(
                    enablement,
                    activity,
                    enablement,
                    activity,
                ),
            ),
            mock.patch.object(
                MODULE,
                "audit_active_skills",
                return_value=[],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report = MODULE.scheduler_report(self.home, "linux")
            _doctor_report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=False,
            )

        self.assertEqual(report.failure_reason, "scheduled sync failed")
        self.assertIsNone(report.failure_code)
        self.assertFalse(report.enabled)
        assert report.daemon_query is not None
        self.assertEqual(report.daemon_query.classification, "disabled")
        self.assertIn(
            (None, "scheduled sync failed"),
            report.failures,
        )
        self.assertIn(
            (
                "scheduler-daemon-disabled",
                "systemd reports scheduler unit state disabled",
            ),
            report.failures,
        )
        issue_codes = {issue.code for issue in issues}
        self.assertIn("scheduler-failure", issue_codes)
        self.assertIn("scheduler-daemon-disabled", issue_codes)

    def test_scheduler_status_binds_config_across_native_query(self) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = self.root / f"status-{platform_name}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                    target = (
                        paths.launchd_plist
                        if platform_name == "macos"
                        else paths.systemd_service
                    )
                    assert target is not None
                    original = target.read_text(encoding="utf-8")

                    def mutate_during_status(
                        args: list[str],
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[str]:
                        target.write_text(
                            original.replace(
                                "owner/public-sync",
                                "owner/changed-sync",
                            ),
                            encoding="utf-8",
                        )
                        target.chmod(0o600)
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            "enabled\n",
                            "",
                        )

                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=mutate_during_status,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        ),
                    ):
                        report = MODULE.scheduler_report(
                            case_home,
                            platform_name,
                        )

                self.assertEqual(report.failure_code, "scheduler-config-drift")
                self.assertIsNone(report.enabled)
                self.assertIn("content changed", report.failure_reason or "")
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )
                self.assertIn(
                    "scheduler-config-drift",
                    {code for code, _reason in report.failures},
                )
                self.assertIn(
                    "scheduler-daemon-unavailable",
                    {code for code, _reason in report.failures},
                )

    def test_scheduler_status_binds_activation_absence_across_native_query(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"activation-status-{platform_name}" / "home"
                )
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                    marker = MODULE._scheduler_activation_transaction_path(paths)
                    self.assertFalse(marker.exists())

                    def begin_activation_during_status(
                        args: list[str],
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[str]:
                        marker.write_bytes(
                            MODULE._scheduler_activation_transaction_payload(
                                case_home,
                                paths,
                            )
                        )
                        marker.chmod(0o600)
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            "enabled\n",
                            "",
                        )

                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=begin_activation_during_status,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        ),
                    ):
                        report = MODULE.scheduler_report(
                            case_home,
                            platform_name,
                        )

                self.assertEqual(
                    report.failure_code,
                    "scheduler-activation-incomplete",
                )
                self.assertIsNone(report.enabled)
                self.assertIn("appeared", report.failure_reason or "")
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )
                failure_codes = {code for code, _reason in report.failures}
                self.assertIn(
                    "scheduler-activation-incomplete",
                    failure_codes,
                )
                self.assertNotIn(
                    "scheduler-daemon-unavailable",
                    failure_codes,
                )

    def test_scheduler_status_allows_mtime_churn_and_reports_unavailable(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    "owner/public-sync",
                    17,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_timer
                )
                assert target is not None

                def touch_during_status(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    metadata = target.stat()
                    os.utime(
                        target,
                        ns=(
                            metadata.st_atime_ns,
                            metadata.st_mtime_ns + 1_000_000,
                        ),
                    )
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        (
                            "active\n"
                            if len(args) > 2 and args[2] == "is-active"
                            else "enabled\n"
                        ),
                        "",
                    )

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_current_releases_for_scheduler",
                            return_value=(),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_scheduler_release_integrity_issues",
                            return_value=(),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_quarantine_batch_count",
                            return_value=0,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_run_bounded_scheduler_process",
                            side_effect=touch_during_status,
                        )
                    )
                    healthy = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertTrue(healthy.enabled)
                self.assertIsNone(healthy.failure_code)

                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_stable_scheduler_runner_matches",
                        return_value=True,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_current_releases_for_scheduler",
                        return_value=(),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_scheduler_release_integrity_issues",
                        return_value=(),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_quarantine_batch_count",
                        return_value=0,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_bounded_scheduler_process",
                        side_effect=OSError("status unavailable"),
                    ),
                ):
                    unavailable = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertIsNone(unavailable.enabled)
                self.assertEqual(
                    unavailable.failure_code,
                    "scheduler-daemon-unavailable",
                )
                assert unavailable.daemon_query is not None
                self.assertEqual(
                    unavailable.daemon_query.classification,
                    "unavailable",
                )

    def test_linux_install_binds_semantically_audited_pair(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "linux")
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        original_timer = paths.systemd_timer.read_bytes()
        real_audit = MODULE._audit_scheduler_config
        replacement = b"user edited systemd service\n"

        def audit_then_edit(
            selected_paths: MODULE.SchedulerPaths,
        ) -> MODULE.SchedulerConfigAudit:
            audit = real_audit(selected_paths)
            paths.systemd_service.write_bytes(replacement)
            paths.systemd_service.chmod(0o600)
            return audit

        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                side_effect=audit_then_edit,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")

        self.assertEqual(paths.systemd_service.read_bytes(), replacement)
        self.assertEqual(paths.systemd_timer.read_bytes(), original_timer)
        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())

    def test_scheduler_exchange_never_restores_a_replaced_displaced_path(
        self,
    ) -> None:
        paths = (
            self.user_home
            / "Library"
            / "LaunchAgents"
            / "com.openai.codex-personal-sync.plist",
            self.user_home
            / ".config"
            / "systemd"
            / "user"
            / "codex-personal-sync.service",
        )
        for config_path in paths:
            with self.subTest(path=config_path):
                config_path.parent.mkdir(parents=True, exist_ok=True)
                original = f"original:{config_path.name}\n".encode()
                replacement = f"replacement:{config_path.name}\n".encode()
                attacker = f"attacker:{config_path.name}\n".encode()
                config_path.write_bytes(original)
                config_path.chmod(0o600)
                original_identity = (
                    config_path.stat().st_dev,
                    config_path.stat().st_ino,
                )
                expected = MODULE._scheduler_config_snapshot(config_path)
                real_exchange = MODULE._rename_exchange_at
                exchange_count = 0

                def exchange_then_replace_displaced(
                    first_parent_fd: int,
                    first_name: str,
                    second_parent_fd: int,
                    second_name: str,
                ) -> None:
                    nonlocal exchange_count
                    real_exchange(
                        first_parent_fd,
                        first_name,
                        second_parent_fd,
                        second_name,
                    )
                    exchange_count += 1
                    if exchange_count == 1:
                        displaced_path = config_path.with_name(first_name)
                        displaced_path.unlink()
                        displaced_path.write_bytes(attacker)
                        displaced_path.chmod(0o600)

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_exchange_at",
                        side_effect=exchange_then_replace_displaced,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "trusted staged config remains live.*exact original "
                        "is preserved",
                    ),
                ):
                    MODULE._atomic_write_scheduler_config(
                        config_path,
                        replacement,
                        expected_snapshot=expected,
                    )

                self.assertEqual(exchange_count, 1)
                self.assertEqual(config_path.read_bytes(), replacement)
                self.assertNotEqual(config_path.read_bytes(), attacker)
                recovery_paths = list(
                    config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*.original"
                    )
                )
                self.assertEqual(len(recovery_paths), 1)
                self.assertEqual(recovery_paths[0].read_bytes(), original)
                self.assertEqual(
                    (
                        recovery_paths[0].stat().st_dev,
                        recovery_paths[0].stat().st_ino,
                    ),
                    original_identity,
                )
                displaced_paths = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced_paths), 1)
                self.assertEqual(displaced_paths[0].read_bytes(), attacker)

    def test_scheduler_cleanup_removes_recovery_before_displaced_preimage(
        self,
    ) -> None:
        config_path = (
            self.user_home
            / "Library"
            / "LaunchAgents"
            / "com.openai.codex-personal-sync.plist"
        )
        config_path.parent.mkdir(parents=True)
        original = b"original scheduler config\n"
        replacement = b"replacement scheduler config\n"
        config_path.write_bytes(original)
        config_path.chmod(0o600)
        expected = MODULE._scheduler_config_snapshot(config_path)
        cleanup_labels: list[str] = []
        real_cleanup = MODULE._isolate_and_delete_pending_cleanup_file

        def observe_cleanup(
            home: Path,
            path: Path,
            parent_fd: int,
            snapshot: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            cleanup_labels.append(label)
            if "recovery evidence" in label:
                displaced = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced), 1)
                self.assertEqual(displaced[0].read_bytes(), original)
            real_cleanup(
                home,
                path,
                parent_fd,
                snapshot,
                label=label,
            )

        with mock.patch.object(
            MODULE,
            "_isolate_and_delete_pending_cleanup_file",
            side_effect=observe_cleanup,
        ):
            installed = MODULE._atomic_write_scheduler_config(
                config_path,
                replacement,
                expected_snapshot=expected,
            )

        self.assertEqual(installed.payload, replacement)
        self.assertEqual(config_path.read_bytes(), replacement)
        scheduler_cleanup_labels = [
            label for label in cleanup_labels if label.startswith("scheduler config ")
        ]
        self.assertEqual(
            scheduler_cleanup_labels,
            [
                f"scheduler config recovery evidence {config_path}",
                f"scheduler config backup {config_path}",
            ],
        )

    def test_scheduler_recovery_cleanup_races_preserve_displaced_preimage(
        self,
    ) -> None:
        for race in ("unlink", "replace"):
            with self.subTest(race=race):
                config_path = self.user_home / race / "codex-personal-sync.service"
                config_path.parent.mkdir(parents=True)
                original = f"original:{race}\n".encode()
                replacement = f"replacement:{race}\n".encode()
                config_path.write_bytes(original)
                config_path.chmod(0o600)
                expected = MODULE._scheduler_config_snapshot(config_path)
                real_cleanup = MODULE._isolate_and_delete_pending_cleanup_file
                injected = False

                def race_recovery_cleanup(
                    home: Path,
                    path: Path,
                    parent_fd: int,
                    snapshot: MODULE.ManagedStateFileSnapshot,
                    *,
                    label: str,
                ) -> None:
                    nonlocal injected
                    if "recovery evidence" in label and not injected:
                        injected = True
                        path.unlink()
                        if race == "replace":
                            # Preserve content and access while replacing the
                            # named object identity.
                            path.write_bytes(original)
                            path.chmod(0o600)
                    real_cleanup(
                        home,
                        path,
                        parent_fd,
                        snapshot,
                        label=label,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                        side_effect=race_recovery_cleanup,
                    ),
                    self.assertRaises(MODULE.SyncError),
                ):
                    MODULE._atomic_write_scheduler_config(
                        config_path,
                        replacement,
                        expected_snapshot=expected,
                    )

                self.assertTrue(injected)
                self.assertEqual(config_path.read_bytes(), replacement)
                displaced = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced), 1)
                self.assertEqual(displaced[0].read_bytes(), original)

    def test_scheduler_recovery_link_survives_parent_fsync_failure(self) -> None:
        config_path = (
            self.user_home
            / ".config"
            / "systemd"
            / "user"
            / "codex-personal-sync.service"
        )
        config_path.parent.mkdir(parents=True)
        original = b"original scheduler config\n"
        config_path.write_bytes(original)
        config_path.chmod(0o600)
        expected = MODULE._scheduler_config_snapshot(config_path)
        real_fsync = MODULE.os.fsync
        parent_fsyncs = 0

        def fail_recovery_parent_fsync(file_fd: int) -> None:
            nonlocal parent_fsyncs
            if stat.S_ISDIR(os.fstat(file_fd).st_mode):
                parent_fsyncs += 1
                if parent_fsyncs == 2:
                    raise OSError("injected recovery parent fsync failure")
            real_fsync(file_fd)

        with (
            mock.patch.object(
                MODULE.os,
                "fsync",
                side_effect=fail_recovery_parent_fsync,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "failed to publish scheduler config",
            ),
        ):
            MODULE._atomic_write_scheduler_config(
                config_path,
                b"replacement scheduler config\n",
                expected_snapshot=expected,
            )

        recovery_paths = list(
            config_path.parent.glob(
                f".{config_path.name}.personal-sync-write-*.original"
            )
        )
        self.assertEqual(len(recovery_paths), 1)
        self.assertEqual(recovery_paths[0].read_bytes(), original)
        self.assertEqual(config_path.read_bytes(), original)

    def test_matching_scheduler_revalidates_semantic_audit_before_daemon(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    "owner/current",
                    17,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                real_audit = MODULE._audit_scheduler_config
                replacement = f"user edited {platform_name} config\n".encode()

                def audit_then_edit(
                    selected_paths: MODULE.SchedulerPaths,
                ) -> MODULE.SchedulerConfigAudit:
                    audit = real_audit(selected_paths)
                    target.write_bytes(replacement)
                    target.chmod(0o600)
                    return audit

                with (
                    mock.patch.object(
                        MODULE,
                        "_audit_scheduler_config",
                        side_effect=audit_then_edit,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                    ) as run_native,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "changed after semantic audit",
                    ),
                ):
                    self.install_scheduler_quietly(
                        "owner/current",
                        None,
                        platform_name,
                        enable=True,
                    )

                run_native.assert_not_called()
                self.assertEqual(target.read_bytes(), replacement)

    def test_scheduler_after_snapshot_ignores_non_access_bearing_group(self) -> None:
        payload = b"service\n"
        wrong_group = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=payload,
            mode=0o600,
            file_identity=(1, 2),
            file_type=stat.S_IFREG,
            size=len(payload),
            uid=os.geteuid(),
            gid=os.getegid() + 1,
        )
        self.assertTrue(
            MODULE._scheduler_snapshot_matches_after_payload(
                wrong_group,
                payload,
            )
        )
        expected_group = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=payload,
            mode=0o600,
            file_identity=(1, 3),
            file_type=stat.S_IFREG,
            size=len(payload),
            uid=os.geteuid(),
            gid=os.getegid(),
        )
        self.assertTrue(
            MODULE._scheduler_snapshot_matches_after_payload(
                expected_group,
                payload,
            )
        )

    def test_scheduler_rollback_restores_before_group_in_setgid_parent(
        self,
    ) -> None:
        caller_groups = {os.getegid(), *os.getgroups()}
        alternate_groups = sorted(caller_groups - {os.getegid()})
        if not alternate_groups:
            self.skipTest("caller has no alternate group for setgid regression")
        parent_gid = alternate_groups[0]
        config_parent = self.home / "scheduler-group-rollback"
        config_parent.mkdir()
        try:
            os.chown(config_parent, -1, parent_gid)
            config_parent.chmod(0o2755)
        except PermissionError as error:
            self.skipTest(f"cannot prepare setgid regression directory: {error}")
        if config_parent.stat().st_gid != parent_gid:
            self.skipTest("filesystem did not preserve requested parent group")

        config = config_parent / "service.conf"
        config.write_bytes(b"after\n")
        config.chmod(0o600)
        current = MODULE._scheduler_config_snapshot(config)
        if current.gid != parent_gid:
            self.skipTest("filesystem did not inherit setgid parent group")
        desired_payload = b"before\n"
        desired = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=desired_payload,
            mode=0o640,
            file_identity=(1, 1),
            file_type=stat.S_IFREG,
            size=len(desired_payload),
            uid=os.geteuid(),
            gid=os.getegid(),
        )

        MODULE._restore_scheduler_config_snapshot(
            config,
            current=current,
            desired=desired,
        )

        restored = MODULE._scheduler_config_snapshot(config)
        self.assertTrue(MODULE._scheduler_file_logical_state_matches(restored, desired))

    def test_scheduler_report_requires_every_configured_current(self) -> None:
        public_config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "public.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        private_config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "private.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="private",
            repo="owner/private-sync",
            base_repo="owner/public-sync",
            owner="private",
        )

        cases = (
            (
                "public",
                public_config,
                lambda _home, _owner: None,
                MODULE.PUBLIC_OWNER,
            ),
            (
                "private",
                private_config,
                lambda _home, owner: (
                    PUBLIC_SHA if owner == MODULE.PUBLIC_OWNER else None
                ),
                "private",
            ),
        )
        for label, config, current_sha, expected_owner in cases:
            with (
                self.subTest(label=label),
                mock.patch.object(
                    MODULE,
                    "_audit_scheduler_config",
                    return_value=MODULE.SchedulerConfigAudit(
                        config=config,
                        snapshots=(),
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "_retain_scheduler_config_audit_bindings",
                    return_value=contextlib.nullcontext(()),
                ),
                mock.patch.object(
                    MODULE,
                    "_retain_launchd_activation_binding",
                    return_value=contextlib.nullcontext(
                        mock.sentinel.activation_binding
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "_read_scheduler_runtime_state",
                    return_value=None,
                ),
                mock.patch.object(
                    MODULE,
                    "_current_sha",
                    side_effect=current_sha,
                ),
                mock.patch.object(
                    MODULE,
                    "_quarantine_batch_count",
                    return_value=0,
                ),
                mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=True,
                ),
            ):
                report = MODULE.scheduler_report(self.home, "linux")

            self.assertEqual(report.failure_code, "current-release-missing")
            self.assertIn(expected_owner, report.failure_reason or "")
            self.assertEqual(report.current_releases, ())

    def test_quarantine_audit_preserves_prior_codeless_runtime_failure(
        self,
    ) -> None:
        config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "scheduler.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        runtime_state = {
            "version": 2,
            "last_attempt": "2026-07-23T09:00:00+00:00",
            "last_success": None,
            "success": False,
            "failure_reason": "legacy failure without a code",
            "failure_code": None,
            "release_trees": {},
            "mode": "public",
            "repo": "owner/public-sync",
            "base_repo": "owner/public-sync",
            "owner": MODULE.PUBLIC_OWNER,
        }
        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                return_value=MODULE.SchedulerConfigAudit(
                    config=config,
                    snapshots=(),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_retain_scheduler_config_audit_bindings",
                return_value=contextlib.nullcontext(()),
            ),
            mock.patch.object(
                MODULE,
                "_retain_launchd_activation_binding",
                return_value=contextlib.nullcontext(mock.sentinel.activation_binding),
            ),
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                side_effect=MODULE.SyncError("audit unavailable"),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                return_value=True,
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            report.failure_reason,
            "legacy failure without a code",
        )
        self.assertIsNone(report.failure_code)

    def test_runtime_v1_is_normalized_and_failure_code_is_persisted(
        self,
    ) -> None:
        previous_success = "2026-07-22T08:00:00+00:00"
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 1,
                "last_attempt": previous_success,
                "last_success": previous_success,
                "success": True,
                "failure_reason": None,
                "mode": "public",
                "repo": "owner/public-sync",
                "base_repo": "owner/public-sync",
                "owner": MODULE.PUBLIC_OWNER,
            },
        )
        normalized = MODULE._read_scheduler_runtime_state(self.home)
        assert normalized is not None
        self.assertEqual(normalized["version"], 2)
        self.assertIsNone(normalized["failure_code"])

        with (
            mock.patch.object(
                MODULE,
                "install_from_github",
                side_effect=MODULE.SyncError(
                    "existing release tree differs",
                    code="immutable-release-drift",
                ),
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/public-sync",
                mode="public",
                base_repo="owner/ignored",
                owner="private",
            )
        failed = MODULE._read_scheduler_runtime_state(self.home)
        assert failed is not None
        self.assertEqual(failed["version"], 2)
        self.assertEqual(failed["failure_code"], "immutable-release-drift")
        self.assertEqual(failed["last_success"], previous_success)

    def test_doctor_reports_quarantine_saturation_without_mutation(self) -> None:
        quarantine = self.home / "personal-sync" / MODULE.QUARANTINE_RELATIVE_PATH
        quarantine.mkdir(parents=True)
        for index in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES):
            prefix = MODULE.PENDING_CLEANUP_ISOLATED_BATCH_PREFIX if index % 2 else ""
            (quarantine / f"{prefix}20260723T000000Z-{index + 1}-{index + 1}").mkdir()
        before = snapshot_tree(quarantine)

        with contextlib.redirect_stdout(io.StringIO()):
            report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        self.assertEqual(
            report.quarantine_batches,
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES,
        )
        saturated = [issue for issue in issues if issue.code == "quarantine-saturated"]
        self.assertEqual(len(saturated), 1)
        self.assertIn(
            f">= {MODULE.MAX_RETAINED_QUARANTINE_BATCHES}",
            saturated[0].detail,
        )
        self.assertEqual(snapshot_tree(quarantine), before)

    def test_doctor_reports_mirror_quarantine_recovery_without_mutation(
        self,
    ) -> None:
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        for index in range(2):
            evidence = quarantine / f"transient-evidence-{index}"
            evidence.write_bytes(f"evidence {index}\n".encode())
            evidence.chmod(0o600)

        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.0123456789abcdef0123456789abcdef"
        )
        owner_name = f"{private_name}.owner.json"
        owner_nonce = "fedcba9876543210fedcba9876543210"
        expected_private_identity = [123, 456, stat.S_IFDIR]
        owner_path = tool_root / owner_name
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": owner_nonce,
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": expected_private_identity,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)
        real_bind = MODULE._bind_mirror_primary_control_parent
        observed_bind_paths = []

        def reject_default_host_parent(spec):
            candidate = Path(os.path.abspath(spec.parent_path))
            self.assertNotEqual(
                candidate,
                self.host_mirror_private_control_parent,
            )
            observed_bind_paths.append(candidate)
            return real_bind(spec)

        doctor_output = io.StringIO()
        strict_output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "MIRROR_DURABLE_QUARANTINE_ENTRY_LIMIT",
                2,
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_primary_control_parent",
                side_effect=reject_default_host_parent,
            ),
        ):
            with contextlib.redirect_stdout(doctor_output):
                report, issues = MODULE.doctor(
                    self.home,
                    "linux",
                    json_output=True,
                )
            with contextlib.redirect_stdout(strict_output):
                strict_status = MODULE.main(
                    [
                        "status-scheduler",
                        "--home",
                        str(self.home),
                        "--platform",
                        "linux",
                        "--json",
                        "--strict",
                    ]
                )

        self.assertEqual(strict_status, 1)
        audit = report.mirror_quarantine
        assert audit is not None
        self.assertEqual(audit.classification, "saturated")
        self.assertEqual(audit.entry_count, 2)
        self.assertEqual(audit.entry_limit, 2)
        self.assertFalse(audit.count_is_lower_bound)
        self.assertEqual(
            audit.segment_identity,
            MODULE._mirror_object_identity(quarantine.stat()),
        )
        self.assertEqual(len(audit.owner_records), 1)
        owner_record = audit.owner_records[0]
        self.assertEqual(owner_record.name, owner_name)
        self.assertEqual(owner_record.state, "stale")
        self.assertEqual(owner_record.owner_nonce, owner_nonce)
        self.assertEqual(owner_record.phase, "cleanup")
        self.assertEqual(owner_record.private_name, private_name)
        self.assertEqual(
            owner_record.expected_private_identity,
            tuple(expected_private_identity),
        )
        self.assertEqual(owner_record.private_state, "missing")
        self.assertIsNotNone(owner_record.sha256)
        self.assertIn(
            "mirror-quarantine-saturated",
            {issue.code for issue in issues},
        )
        payload = json.loads(doctor_output.getvalue())
        self.assertEqual(
            payload["scheduler"]["mirror_quarantine"]["classification"],
            "saturated",
        )
        self.assertEqual(
            payload["scheduler"]["mirror_quarantine"]["owner_records"][0][
                "owner_nonce"
            ],
            owner_nonce,
        )
        self.assertEqual(
            observed_bind_paths,
            [
                self.mirror_private_control_parent,
                self.mirror_private_control_parent,
                self.mirror_private_control_parent,
                self.mirror_private_control_parent,
            ],
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_scheduler_status_reports_mirror_quarantine_audit_inconclusive(
        self,
    ) -> None:
        (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        ).mkdir(mode=0o700)
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        quarantine.chmod(0o755)
        before = snapshot_tree(self.mirror_private_control_parent)

        with contextlib.redirect_stdout(io.StringIO()):
            report = MODULE.status_scheduler(
                self.home,
                "linux",
                json_output=True,
            )

        audit = report.mirror_quarantine
        assert audit is not None
        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn(
            "must be mode 0700",
            audit.detail,
        )
        self.assertIn(
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
            {code for code, _detail in report.failures},
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_registry_skips_foreign_legacy_without_opening_it(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-shared-parent"
        shared_parent.mkdir(mode=0o700)
        shared_parent.chmod(0o1777)
        preclaimed = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        preclaimed.mkdir(mode=0o700)
        preclaimed.chmod(0o000)
        before = preclaimed.lstat()
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_metadata = MODULE._mirror_legacy_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory
        legacy_child_opens = []
        legacy_metadata_calls: dict[str, int] = {}

        def classify_preclaim_as_foreign(parent_fd, name, root_id):
            legacy_metadata_calls[name] = legacy_metadata_calls.get(name, 0) + 1
            observed = real_metadata(parent_fd, name, root_id)
            if name == MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME and observed is not None:
                identity, access_policy = observed
                return identity, (
                    access_policy[0],
                    os.geteuid() + 1,
                    access_policy[2],
                )
            return observed

        def reject_legacy_child_open(parent_fd, parent_path, name, label):
            if parent_path == shared_parent:
                legacy_child_opens.append(name)
                self.fail(f"foreign legacy child was opened: {name}")
            return real_bind_child(parent_fd, parent_path, name, label)

        observed_after = None
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MODULE,
                    "_mirror_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MODULE,
                    "_mirror_legacy_child_metadata",
                    side_effect=classify_preclaim_as_foreign,
                ),
                mock.patch.object(
                    MODULE,
                    "_bind_mirror_audit_child_directory",
                    side_effect=reject_legacy_child_open,
                ),
            ):
                audit = MODULE._mirror_quarantine_audit()
            observed_after = preclaimed.lstat()
        finally:
            preclaimed.chmod(0o700)

        self.assertEqual(audit.classification, "absent")
        self.assertEqual(
            [root.root_id for root in audit.root_audits],
            [primary_spec.root_id, legacy_spec.root_id],
        )
        self.assertEqual(audit.root_audits[1].classification, "foreign-unrelated")
        self.assertEqual(
            legacy_metadata_calls,
            {
                MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME: 3,
                MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME: 3,
            },
        )
        payload = MODULE._mirror_quarantine_payload(audit)
        assert payload is not None
        self.assertEqual(payload["roots"][1]["root_id"], legacy_spec.root_id)
        self.assertEqual(legacy_child_opens, [])
        assert observed_after is not None
        self.assertEqual(
            (
                observed_after.st_dev,
                observed_after.st_ino,
                stat.S_IMODE(observed_after.st_mode),
            ),
            (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode)),
        )

    def test_mirror_quarantine_terminal_revalidation_covers_every_root(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_specs = []
        for index in range(2):
            parent = self.root / f"terminal-legacy-{index}"
            parent.mkdir(mode=0o700)
            legacy_specs.append(
                MODULE.MirrorPrivateControlRootSpec(
                    root_id=f"terminal-legacy-{index}",
                    parent_path=parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
            )
        early_tool = legacy_specs[0].parent_path / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        early_tool.mkdir(mode=0o700)
        real_root_audit = MODULE._mirror_quarantine_root_audit
        real_metadata = MODULE._mirror_legacy_child_metadata
        metadata_calls: dict[tuple[str, str], int] = {}
        mutated = False

        def count_metadata(parent_fd, name, root_id):
            key = (root_id, name)
            metadata_calls[key] = metadata_calls.get(key, 0) + 1
            return real_metadata(parent_fd, name, root_id)

        def mutate_early_root_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            nonlocal mutated
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == legacy_specs[-1].root_id:
                early_tool.chmod(0o755)
                mutated = True
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, *legacy_specs),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_child_metadata",
                side_effect=count_metadata,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=mutate_early_root_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertTrue(mutated)
        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
        )
        self.assertIn(
            "terminal mirror quarantine registry revalidation covered every root",
            audit.detail,
        )
        for spec in (primary_spec, *legacy_specs):
            self.assertIn(spec.root_id, audit.detail)
        self.assertEqual(audit.root_audits[1].classification, "inconclusive")
        self.assertGreaterEqual(
            metadata_calls[
                (
                    legacy_specs[-1].root_id,
                    MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME,
                )
            ],
            3,
        )

    def test_mirror_quarantine_terminal_revalidates_absent_parent_anchor(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        absent_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-absent-legacy",
            parent_path=self.root / "terminal-absent-legacy",
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        last_parent = self.root / "terminal-last-legacy"
        last_parent.mkdir(mode=0o700)
        last_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-last-legacy",
            parent_path=last_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_root_audit = MODULE._mirror_quarantine_root_audit

        def create_absent_parent_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == last_spec.root_id:
                absent_spec.parent_path.mkdir(mode=0o700)
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, absent_spec, last_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=create_absent_parent_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
        )
        self.assertIn(absent_spec.root_id, audit.detail)
        self.assertIn("is no longer absent", audit.detail)

    def test_mirror_quarantine_terminal_revalidates_primary_absence_anchor(
        self,
    ) -> None:
        account_home = self.root / "terminal-primary-home"
        account_home.mkdir(mode=0o700)
        primary_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-absent-primary",
            parent_path=(account_home / MODULE.MIRROR_PRIVATE_CONTROL_NAMESPACE_NAME),
            allocate=True,
            account_home=account_home,
            shared_parent=False,
        )
        last_parent = self.root / "terminal-primary-last-legacy"
        last_parent.mkdir(mode=0o700)
        last_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-primary-last-legacy",
            parent_path=last_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_root_audit = MODULE._mirror_quarantine_root_audit

        def create_primary_parent_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == last_spec.root_id:
                primary_spec.parent_path.mkdir(mode=0o700)
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, last_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=create_primary_parent_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
        )
        self.assertIn(primary_spec.root_id, audit.detail)
        self.assertIn("is no longer absent", audit.detail)

    def test_mirror_quarantine_terminal_duplicate_revalidates_only_parent(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        shared_parent = self.root / "terminal-duplicate-parent"
        shared_parent.mkdir(mode=0o700)
        first_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-original-legacy",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        duplicate_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-duplicate-legacy",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_metadata = MODULE._mirror_legacy_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory
        metadata_root_ids: list[str] = []
        duplicate_child_opens: list[str] = []

        def record_metadata_root(parent_fd, name, root_id):
            metadata_root_ids.append(root_id)
            return real_metadata(parent_fd, name, root_id)

        def reject_duplicate_child_open(parent_fd, parent_path, name, label):
            if parent_path == shared_parent:
                duplicate_child_opens.append(name)
                self.fail(f"duplicate root child was opened: {name}")
            return real_bind_child(parent_fd, parent_path, name, label)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, first_spec, duplicate_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_child_metadata",
                side_effect=record_metadata_root,
            ),
            mock.patch.object(
                MODULE,
                "_bind_mirror_audit_child_directory",
                side_effect=reject_duplicate_child_open,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.root_audits[2].classification, "duplicate")
        self.assertNotIn(duplicate_spec.root_id, metadata_root_ids)
        self.assertEqual(duplicate_child_opens, [])

    def test_mirror_quarantine_foreign_topology_is_metadata_only(self) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        real_metadata = MODULE._mirror_legacy_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory
        for topology_case in ("different-device", "inverted-containment"):
            with self.subTest(topology_case=topology_case):
                shared_parent = self.root / f"foreign-topology-{topology_case}"
                shared_parent.mkdir(mode=0o700)
                tool = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
                quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
                tool.mkdir(mode=0o700)
                quarantine.mkdir(mode=0o700)
                legacy_spec = MODULE.MirrorPrivateControlRootSpec(
                    root_id=f"foreign-topology-{topology_case}",
                    parent_path=shared_parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
                ancestor_identity = MODULE._mirror_object_identity(
                    shared_parent.parent.stat()
                )
                child_opens: list[str] = []

                def foreign_topology_metadata(parent_fd, name, root_id):
                    observed = real_metadata(parent_fd, name, root_id)
                    assert observed is not None
                    identity, access_policy = observed
                    if (
                        topology_case == "different-device"
                        and name == MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
                    ):
                        identity = (identity[0] + 1, identity[1], identity[2])
                    elif (
                        topology_case == "inverted-containment"
                        and name == MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
                    ):
                        identity = ancestor_identity
                    return identity, (
                        access_policy[0],
                        os.geteuid() + 1,
                        access_policy[2],
                    )

                def reject_foreign_child_open(parent_fd, parent_path, name, label):
                    if parent_path == shared_parent:
                        child_opens.append(name)
                        self.fail(f"foreign topology child was opened: {name}")
                    return real_bind_child(parent_fd, parent_path, name, label)

                with (
                    mock.patch.object(
                        MODULE,
                        "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                        (primary_spec, legacy_spec),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_mirror_legacy_shared_parent_policy_is_valid",
                        return_value=True,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_mirror_legacy_child_metadata",
                        side_effect=foreign_topology_metadata,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_bind_mirror_audit_child_directory",
                        side_effect=reject_foreign_child_open,
                    ),
                ):
                    audit = MODULE._mirror_quarantine_audit()

                self.assertEqual(audit.classification, "absent")
                self.assertEqual(
                    audit.root_audits[1].classification,
                    "foreign-unrelated",
                )
                self.assertEqual(
                    MODULE._mirror_private_control_preallocation_decision(
                        ("foreign-unrelated",)
                    ),
                    (True, None),
                )
                self.assertEqual(child_opens, [])

    def test_mirror_quarantine_bound_topology_requires_child_below_parent(
        self,
    ) -> None:
        parent = self.root / "bound-topology-parent"
        child = parent / "bound-topology-child"
        child.mkdir(parents=True, mode=0o700)
        parent_fd = os.open(parent, MODULE._source_directory_flags())
        child_fd = os.open(child, MODULE._source_directory_flags())
        try:
            parent_identity = MODULE._mirror_object_identity(os.fstat(parent_fd))
            child_identity = MODULE._mirror_object_identity(os.fstat(child_fd))
            MODULE._validate_mirror_private_control_bound_topology(
                root_id="valid-topology",
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                tool_fd=child_fd,
                tool_identity=child_identity,
            )
            with self.assertRaisesRegex(MODULE.SyncError, "strict descendant"):
                MODULE._validate_mirror_private_control_bound_topology(
                    root_id="inverted-topology",
                    parent_fd=child_fd,
                    parent_identity=child_identity,
                    tool_fd=parent_fd,
                    tool_identity=parent_identity,
                )
        finally:
            os.close(child_fd)
            os.close(parent_fd)

    def test_mirror_quarantine_bound_topology_requires_same_filesystem(
        self,
    ) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_mirror_directory_is_at_or_below",
                side_effect=(True, False, True, False),
            ),
            mock.patch.object(
                MODULE.os,
                "fstat",
                side_effect=(mock.Mock(st_dev=1), mock.Mock(st_dev=2)),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "same filesystem"),
        ):
            MODULE._validate_mirror_private_control_bound_topology(
                root_id="same-uid-legacy",
                parent_fd=10,
                parent_identity=(1, 10, stat.S_IFDIR),
                tool_fd=11,
                tool_identity=(1, 11, stat.S_IFDIR),
                quarantine_fd=12,
                quarantine_identity=(2, 12, stat.S_IFDIR),
            )

    def test_mirror_quarantine_same_uid_legacy_topology_fails_before_scan(
        self,
    ) -> None:
        shared_parent = self.root / "same-uid-topology-parent"
        shared_parent.mkdir(mode=0o700)
        (shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME).mkdir(mode=0o700)
        (shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME).mkdir(mode=0o700)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="same-uid-topology-legacy",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_validate = MODULE._validate_mirror_private_control_bound_topology

        def reject_same_uid_combined_topology(**kwargs):
            if (
                kwargs["root_id"] == legacy_spec.root_id
                and kwargs.get("quarantine_fd", -1) >= 0
            ):
                raise MODULE.SyncError("synthetic same-uid legacy topology failure")
            return real_validate(**kwargs)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_validate_mirror_private_control_bound_topology",
                side_effect=reject_same_uid_combined_topology,
            ),
            mock.patch.object(MODULE, "_bounded_mirror_directory_names") as inventory,
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn("synthetic same-uid legacy topology failure", audit.detail)
        inventory.assert_not_called()

    def test_mirror_quarantine_does_not_scan_before_full_topology_validation(
        self,
    ) -> None:
        tool = self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        real_validate = MODULE._validate_mirror_private_control_bound_topology

        def reject_combined_topology(**kwargs):
            if kwargs.get("quarantine_fd", -1) >= 0:
                raise MODULE.SyncError("synthetic fixed-root overlap")
            return real_validate(**kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_validate_mirror_private_control_bound_topology",
                side_effect=reject_combined_topology,
            ),
            mock.patch.object(MODULE, "_bounded_mirror_directory_names") as inventory,
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn("synthetic fixed-root overlap", audit.detail)
        inventory.assert_not_called()

    def test_mirror_quarantine_terminal_allows_benign_child_entry_churn(
        self,
    ) -> None:
        tool = self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        last_parent = self.root / "benign-churn-last-root"
        last_parent.mkdir(mode=0o700)
        last_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="benign-churn-last-root",
            parent_path=last_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_root_audit = MODULE._mirror_quarantine_root_audit

        def add_benign_entry_after_last_audit(
            spec,
            seen_parent_identities=None,
            seen_child_identities=None,
        ):
            root_audit = real_root_audit(
                spec,
                seen_parent_identities,
                seen_child_identities,
            )
            if spec.root_id == last_spec.root_id:
                (tool / "benign-child-entry").mkdir(mode=0o700)
            return root_audit

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, last_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_mirror_quarantine_root_audit",
                side_effect=add_benign_entry_after_last_audit,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertNotEqual(audit.classification, "inconclusive")

    def test_mirror_quarantine_registry_reports_same_uid_legacy_pending(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-current-parent"
        shared_parent.mkdir(mode=0o700)
        shared_parent.chmod(0o1777)
        (shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME).mkdir(mode=0o700)
        quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        quarantine.mkdir(mode=0o700)
        evidence = quarantine / "retained-evidence"
        evidence.write_bytes(b"retained\n")
        evidence.chmod(0o600)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        before = snapshot_tree(shared_parent)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        self.assertEqual(audit.root_id, legacy_spec.root_id)
        self.assertEqual(audit.entry_count, 1)
        self.assertIn("original root", audit.detail)
        self.assertEqual(snapshot_tree(shared_parent), before)

    def test_mirror_primary_alias_matrix_is_rejected_before_child_open(
        self,
    ) -> None:
        tool = self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        parent_identity = MODULE._mirror_object_identity(
            self.mirror_private_control_parent.stat()
        )
        real_metadata = MODULE._mirror_private_control_child_metadata
        real_bind_child = MODULE._bind_mirror_audit_child_directory

        for scenario in ("parent-child", "child-child"):
            with self.subTest(scenario=scenario):
                tool_record = None
                child_opens: list[str] = []

                def aliased_metadata(parent_fd, name, label):
                    nonlocal tool_record
                    observed = real_metadata(parent_fd, name, label)
                    assert observed is not None
                    if name == MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME:
                        tool_record = observed
                        if scenario == "parent-child":
                            return parent_identity, observed[1]
                        return observed
                    if scenario == "child-child":
                        assert tool_record is not None
                        return tool_record
                    return observed

                def reject_child_open(parent_fd, parent_path, name, label):
                    if name in {
                        MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME,
                        MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
                    }:
                        child_opens.append(name)
                        self.fail(f"aliased mirror child was opened: {name}")
                    return real_bind_child(parent_fd, parent_path, name, label)

                with (
                    mock.patch.object(
                        MODULE,
                        "_mirror_private_control_child_metadata",
                        side_effect=aliased_metadata,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_bind_mirror_audit_child_directory",
                        side_effect=reject_child_open,
                    ),
                ):
                    audit = MODULE._mirror_quarantine_audit()

                self.assertEqual(audit.classification, "inconclusive")
                self.assertEqual(
                    audit.reason_code,
                    MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
                )
                self.assertRegex(
                    audit.detail,
                    "fixed child roles alias|fixed child aliases its own parent",
                )
                self.assertEqual(child_opens, [])

    def test_mirror_legacy_quarantine_without_tool_is_recovery_pending(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-quarantine-without-tool"
        shared_parent.mkdir(mode=0o700)
        quarantine = shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        quarantine.mkdir(mode=0o700)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id=MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        before = snapshot_tree(shared_parent)

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        legacy_audit = audit.root_audits[1]
        self.assertEqual(legacy_audit.classification, "inconclusive")
        self.assertEqual(
            legacy_audit.reason_code,
            MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        self.assertIn("without its coordination tool root", legacy_audit.detail)
        self.assertEqual(snapshot_tree(shared_parent), before)

    def test_mirror_quarantine_audit_holds_directory_leases_without_mutation(
        self,
    ) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)
        real_inventory = MODULE._bounded_mirror_directory_names
        observed_lock_order: list[str] = []

        def assert_shared_lease(directory_fd, *, limit, label):
            path = tool_root if label == "mirror private tool root" else quarantine
            contender_fd = os.open(path, MODULE._source_directory_flags())
            try:
                with self.assertRaises(BlockingIOError):
                    MODULE.fcntl.flock(
                        contender_fd,
                        MODULE.fcntl.LOCK_EX | MODULE.fcntl.LOCK_NB,
                    )
            finally:
                os.close(contender_fd)
            observed_lock_order.append(label)
            return real_inventory(
                directory_fd,
                limit=limit,
                label=label,
            )

        with mock.patch.object(
            MODULE,
            "_bounded_mirror_directory_names",
            side_effect=assert_shared_lease,
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "available")
        self.assertEqual(audit.entry_count, 0)
        self.assertEqual(
            observed_lock_order,
            [
                "mirror private tool root",
                "mirror durable quarantine segment",
            ],
        )
        self.assertEqual(len(audit.owner_records), 1)
        self.assertEqual(audit.owner_records[0].state, "stale")
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_retains_cross_root_owner_record(self) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.cccccccccccccccccccccccccccccccc"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": "wrong-primary-root-v9",
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "dddddddddddddddddddddddddddddddd",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)

        audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "inconclusive")
        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(len(audit.owner_records), 1)
        self.assertEqual(
            audit.owner_records[0].reason_code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

        report = MODULE.scheduler_report(self.home, "linux")
        report_audit = report.mirror_quarantine
        assert report_audit is not None
        failure_detail = MODULE._mirror_quarantine_failure_detail(report_audit)
        self.assertIn(
            (
                MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
                failure_detail,
            ),
            report.failures,
        )
        with (
            mock.patch.object(MODULE, "scheduler_report", return_value=report),
            mock.patch.object(MODULE, "audit_active_skills", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _doctor_report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        matching = [issue for issue in issues if issue.detail == failure_detail]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0].code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(matching[0].path, report_audit.path)
        self.assertNotEqual(matching[0].path, self.home)
        self.assertNotIn(
            "scheduler-failure",
            {issue.code for issue in matching},
        )

    def test_mirror_walkers_transfer_fd_before_effectful_close_error(self) -> None:
        real_close = MODULE.os.close

        def invoke_ancestry() -> None:
            candidate_fd = os.open(
                self.mirror_private_control_parent,
                MODULE._source_directory_flags(),
            )
            try:
                MODULE._mirror_directory_is_at_or_below(
                    candidate_fd,
                    (-1, -1, stat.S_IFDIR),
                    label="fault-injected candidate",
                )
            finally:
                os.close(candidate_fd)

        for label, invoke in (
            (
                "account-home",
                lambda: MODULE._bind_mirror_trusted_account_home(self.root),
            ),
            ("ancestry", invoke_ancestry),
        ):
            with self.subTest(label=label):
                close_calls: list[int] = []
                failed_fd: int | None = None

                def fail_first_close_after_effect(descriptor: int) -> None:
                    nonlocal failed_fd
                    close_calls.append(descriptor)
                    real_close(descriptor)
                    if failed_fd is None:
                        failed_fd = descriptor
                        raise OSError(f"injected {label} close-after-effect")

                with (
                    mock.patch.object(
                        MODULE.os,
                        "close",
                        side_effect=fail_first_close_after_effect,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        f"injected {label} close-after-effect",
                    ),
                ):
                    invoke()

                assert failed_fd is not None
                self.assertEqual(close_calls.count(failed_fd), 1)
                self.assertGreaterEqual(len(set(close_calls)), 2)

    def test_owner_audit_aggregates_unlock_and_effectful_close_errors(self) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        )
        owner_name = f"{private_name}.owner.json"
        owner_path = tool_root / owner_name
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "ffffffffffffffffffffffffffffffff",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        owner_identity = MODULE._mirror_object_identity(owner_path.stat())
        tool_fd = os.open(tool_root, MODULE._source_directory_flags())
        real_flock = MODULE.fcntl.flock
        real_close = MODULE.os.close
        owner_close_calls: list[int] = []

        def fail_owner_unlock(descriptor: int, operation: int) -> None:
            if (
                operation == MODULE.fcntl.LOCK_UN
                and MODULE._mirror_object_identity(os.fstat(descriptor))
                == owner_identity
            ):
                raise OSError("injected owner unlock failure")
            real_flock(descriptor, operation)

        def fail_owner_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if descriptor_identity == owner_identity:
                owner_close_calls.append(descriptor)
                raise OSError("injected owner close-after-effect")

        try:
            with (
                mock.patch.object(
                    MODULE.fcntl,
                    "flock",
                    side_effect=fail_owner_unlock,
                ),
                mock.patch.object(
                    MODULE.os,
                    "close",
                    side_effect=fail_owner_close_after_effect,
                ),
                self.assertRaises(MODULE.SyncError) as caught,
            ):
                MODULE._audit_mirror_owner_record(
                    tool_fd,
                    owner_name,
                    MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                )
        finally:
            os.close(tool_fd)

        self.assertIn("injected owner unlock failure", str(caught.exception))
        self.assertIn("injected owner close-after-effect", str(caught.exception))
        self.assertEqual(len(owner_close_calls), 1)

    def test_initial_root_cleanup_failure_does_not_skip_later_roots(self) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        quarantine_identity = MODULE._mirror_object_identity(quarantine.stat())
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_specs = []
        for index in range(2):
            parent = self.root / f"cleanup-later-root-{index}"
            parent.mkdir(mode=0o700)
            legacy_specs.append(
                MODULE.MirrorPrivateControlRootSpec(
                    root_id=f"cleanup-later-root-{index}",
                    parent_path=parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
            )
        real_acquire = MODULE._acquire_mirror_audit_shared_lock
        real_close = MODULE.os.close
        cleanup_enabled = False
        failed_fds: list[int] = []

        def enable_fault_after_quarantine_lease(descriptor: int, label: str) -> None:
            nonlocal cleanup_enabled
            real_acquire(descriptor, label)
            if label == "mirror durable quarantine segment":
                cleanup_enabled = True

        def fail_quarantine_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if (
                cleanup_enabled
                and descriptor_identity == quarantine_identity
                and not failed_fds
            ):
                failed_fds.append(descriptor)
                raise OSError("injected initial-root close-after-effect")

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, *legacy_specs),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_acquire_mirror_audit_shared_lock",
                side_effect=enable_fault_after_quarantine_lease,
            ),
            mock.patch.object(
                MODULE.os,
                "close",
                side_effect=fail_quarantine_close_after_effect,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(len(failed_fds), 1)
        self.assertEqual(
            [root.root_id for root in audit.root_audits],
            [primary_spec.root_id, *(spec.root_id for spec in legacy_specs)],
        )
        self.assertEqual(audit.root_audits[0].classification, "inconclusive")
        self.assertIn("injected initial-root close-after-effect", audit.detail)
        for spec in legacy_specs:
            self.assertIn(spec.root_id, audit.detail)

    def test_absence_and_terminal_close_failures_retain_body_and_coverage(
        self,
    ) -> None:
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        absence_anchor = self.root / "absence-close-anchor"
        absence_anchor.mkdir(mode=0o700)
        absent_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="absence-close-root",
            parent_path=absence_anchor / "missing-parent",
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        later_parent = self.root / "absence-close-later"
        later_parent.mkdir(mode=0o700)
        later_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="absence-close-later",
            parent_path=later_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        anchor_identity = MODULE._mirror_object_identity(absence_anchor.stat())
        real_close = MODULE.os.close
        absence_faults: list[int] = []

        def fail_absence_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if descriptor_identity == anchor_identity and not absence_faults:
                absence_faults.append(descriptor)
                raise OSError("injected absence close-after-effect")

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, absent_spec, later_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE.os,
                "close",
                side_effect=fail_absence_close_after_effect,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(len(absence_faults), 1)
        self.assertEqual(
            [root.root_id for root in audit.root_audits],
            [primary_spec.root_id, absent_spec.root_id, later_spec.root_id],
        )
        self.assertIn("injected absence close-after-effect", audit.detail)
        self.assertIn(later_spec.root_id, audit.detail)

        first_parent = self.root / "terminal-close-first"
        second_anchor = self.root / "terminal-close-second-anchor"
        first_parent.mkdir(mode=0o700)
        second_anchor.mkdir(mode=0o700)
        first_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-close-first",
            parent_path=first_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        second_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="terminal-close-second",
            parent_path=second_anchor / "missing-parent",
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        first_identity = MODULE._mirror_object_identity(first_parent.stat())
        first_policy = MODULE._mirror_access_policy(first_parent.stat())
        second_receipt = MODULE._capture_mirror_quarantine_parent_absence(second_spec)
        first_audit = MODULE.MirrorQuarantineAudit(
            classification="available",
            path=first_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
            entry_count=0,
            entry_limit=MODULE.MIRROR_DURABLE_QUARANTINE_ENTRY_LIMIT,
            count_is_lower_bound=False,
            segment_identity=None,
            segment_access_policy=None,
            root_id=first_spec.root_id,
            root_receipt=MODULE.MirrorQuarantineRootReceipt(
                root_id=first_spec.root_id,
                parent_path=first_parent,
                scope="parent-only",
                parent_identity=(
                    first_identity[0],
                    first_identity[1] + 1,
                    first_identity[2],
                ),
                parent_access_policy=first_policy,
            ),
        )
        second_audit = MODULE.MirrorQuarantineAudit(
            classification="absent",
            path=second_spec.parent_path / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
            entry_count=0,
            entry_limit=MODULE.MIRROR_DURABLE_QUARANTINE_ENTRY_LIMIT,
            count_is_lower_bound=False,
            segment_identity=None,
            segment_access_policy=None,
            root_id=second_spec.root_id,
            root_receipt=second_receipt,
        )
        terminal_faults: list[int] = []

        def fail_terminal_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MODULE._mirror_object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if descriptor_identity == first_identity and not terminal_faults:
                terminal_faults.append(descriptor)
                raise OSError("injected terminal close-after-effect")

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (first_spec, second_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MODULE.os,
                "close",
                side_effect=fail_terminal_close_after_effect,
            ),
        ):
            updated, detail = MODULE._revalidate_mirror_quarantine_registry(
                [first_audit, second_audit]
            )

        self.assertEqual(len(terminal_faults), 1)
        assert detail is not None
        self.assertIn("identity or access policy changed", updated[0].detail)
        self.assertIn("injected terminal close-after-effect", updated[0].detail)
        self.assertIn(first_spec.root_id, detail)
        self.assertIn(second_spec.root_id, detail)
        self.assertEqual(updated[1].classification, "absent")

    def test_legacy_owner_root_mismatch_routes_exactly_once(self) -> None:
        shared_parent = self.root / "legacy-owner-mismatch"
        shared_parent.mkdir(mode=0o700)
        tool_root = shared_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        tool_root.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.11111111111111111111111111111111"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_text(
            json.dumps(
                {
                    "version": MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": "wrong-legacy-root",
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "22222222222222222222222222222222",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        primary_spec = MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MODULE.MirrorPrivateControlRootSpec(
            root_id="legacy-owner-mismatch",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )

        with (
            mock.patch.object(
                MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                (primary_spec, legacy_spec),
            ),
            mock.patch.object(
                MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
        ):
            audit = MODULE._mirror_quarantine_audit()
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            audit.reason_code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(audit.root_id, legacy_spec.root_id)
        self.assertEqual(
            audit.path,
            shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
        )
        report_audit = report.mirror_quarantine
        assert report_audit is not None
        failure_detail = MODULE._mirror_quarantine_failure_detail(report_audit)
        self.assertIn(
            (
                MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
                failure_detail,
            ),
            report.failures,
        )
        with (
            mock.patch.object(MODULE, "scheduler_report", return_value=report),
            mock.patch.object(MODULE, "audit_active_skills", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _doctor_report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        matching = [issue for issue in issues if issue.detail == failure_detail]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0].code,
            MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertEqual(
            matching[0].path,
            shared_parent / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
        )
        self.assertNotIn(
            "scheduler-failure",
            {issue.code for issue in matching},
        )

    def test_mirror_quarantine_audit_reports_busy_tool_root_without_scanning(
        self,
    ) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        before = snapshot_tree(self.mirror_private_control_parent)
        tool_fd = os.open(tool_root, MODULE._source_directory_flags())
        try:
            MODULE.fcntl.flock(tool_fd, MODULE.fcntl.LOCK_EX)
            audit = MODULE._mirror_quarantine_audit()
        finally:
            MODULE.fcntl.flock(tool_fd, MODULE.fcntl.LOCK_UN)
            os.close(tool_fd)

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIsNone(audit.entry_count)
        self.assertIn("busy with an active writer", audit.detail)
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_does_not_lock_without_coordination_root(
        self,
    ) -> None:
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        before = snapshot_tree(self.mirror_private_control_parent)

        with mock.patch.object(
            MODULE,
            "_acquire_mirror_audit_shared_lock",
        ) as acquire:
            audit = MODULE._mirror_quarantine_audit()

        acquire.assert_not_called()
        self.assertEqual(audit.classification, "inconclusive")
        self.assertIsNone(audit.entry_count)
        self.assertIn(
            "exists without its coordination tool root",
            audit.detail,
        )
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_classifies_deep_owner_json_as_invalid(
        self,
    ) -> None:
        tool_root = (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        )
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        tool_root.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.cccccccccccccccccccccccccccccccc"
        )
        owner_path = tool_root / f"{private_name}.owner.json"
        owner_path.write_bytes(b"[" * 1_200 + b"0" + b"]" * 1_200)
        owner_path.chmod(0o600)
        before = snapshot_tree(self.mirror_private_control_parent)

        audit = MODULE._mirror_quarantine_audit()

        self.assertEqual(audit.classification, "available")
        self.assertEqual(len(audit.owner_records), 1)
        self.assertEqual(audit.owner_records[0].state, "invalid")
        self.assertIn("schema", audit.owner_records[0].detail)
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_audit_detects_final_access_policy_drift(
        self,
    ) -> None:
        (
            self.mirror_private_control_parent / MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME
        ).mkdir(mode=0o700)
        quarantine = (
            self.mirror_private_control_parent
            / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine.mkdir(mode=0o700)
        before = snapshot_tree(self.mirror_private_control_parent)
        real_revalidate = MODULE._revalidate_mirror_audit_directory
        quarantine_revalidations = 0

        def mutate_after_first_quarantine_revalidation(
            path,
            directory_fd,
            identity,
            access_policy,
            label,
        ):
            nonlocal quarantine_revalidations
            real_revalidate(
                path,
                directory_fd,
                identity,
                access_policy,
                label,
            )
            if (
                label == "mirror durable quarantine segment"
                and quarantine_revalidations == 0
            ):
                quarantine.chmod(0o755)
                quarantine_revalidations += 1

        try:
            with mock.patch.object(
                MODULE,
                "_revalidate_mirror_audit_directory",
                side_effect=mutate_after_first_quarantine_revalidation,
            ):
                audit = MODULE._mirror_quarantine_audit()
        finally:
            quarantine.chmod(0o700)

        self.assertEqual(audit.classification, "inconclusive")
        self.assertIn("access policy changed during audit", audit.detail)
        self.assertEqual(
            snapshot_tree(self.mirror_private_control_parent),
            before,
        )

    def test_mirror_quarantine_handoff_lists_only_durable_recovery_records(
        self,
    ) -> None:
        def owner(
            name: str,
            *,
            state: str,
            private_state: str | None,
        ) -> MODULE.MirrorQuarantineOwnerRecord:
            return MODULE.MirrorQuarantineOwnerRecord(
                name=name,
                identity=(1, 2, stat.S_IFREG),
                access_policy=(0o600, os.geteuid(), os.getegid()),
                sha256="a" * 64,
                state=state,
                owner_pid=123,
                owner_nonce="b" * 32,
                phase="cleanup",
                private_name=f"{name}.private",
                expected_private_identity=(3, 4, stat.S_IFDIR),
                observed_private_identity=(
                    (3, 4, stat.S_IFDIR)
                    if private_state in {"matching", "mismatched"}
                    else None
                ),
                private_state=private_state,
            )

        audit = MODULE.MirrorQuarantineAudit(
            classification="saturated",
            path=(
                self.mirror_private_control_parent
                / MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME
            ),
            entry_count=10,
            entry_limit=10,
            count_is_lower_bound=False,
            segment_identity=(5, 6, stat.S_IFDIR),
            segment_access_policy=(0o700, os.geteuid(), os.getegid()),
            owner_records=(
                owner("missing.owner.json", state="stale", private_state="missing"),
                owner(
                    "matching.owner.json",
                    state="stale",
                    private_state="matching",
                ),
                owner(
                    "mismatched.owner.json",
                    state="stale",
                    private_state="mismatched",
                ),
                owner("invalid.owner.json", state="invalid", private_state=None),
                owner("active.owner.json", state="active", private_state=None),
            ),
        )

        detail = MODULE._mirror_quarantine_failure_detail(audit)

        self.assertIn("missing.owner.json", detail)
        self.assertIn("matching.owner.json", detail)
        self.assertIn("sha256=" + "a" * 64, detail)
        self.assertIn("nonce=" + "b" * 32, detail)
        self.assertNotIn("mismatched.owner.json", detail)
        self.assertNotIn("invalid.owner.json", detail)
        self.assertNotIn("active.owner.json", detail)

    def test_native_scheduler_commands_use_closed_environment(self) -> None:
        injected = {
            "LD_PRELOAD": "/tmp/injected.so",
            "LD_LIBRARY_PATH": "/tmp/injected",
            "DYLD_INSERT_LIBRARIES": "/tmp/injected.dylib",
            "BASH_ENV": "/tmp/bash-env",
            "ENV": "/tmp/shell-env",
            "PYTHONPATH": "/tmp/python",
        }
        captured: dict[str, object] = {}
        real_popen = subprocess.Popen

        def capture_popen(args, **kwargs):
            captured.update(kwargs)
            return real_popen(args, **kwargs)

        with (
            mock.patch.dict(MODULE.os.environ, injected, clear=False),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                return_value=[sys.executable, "-c", "pass"],
            ),
            mock.patch.object(
                MODULE.subprocess,
                "Popen",
                side_effect=capture_popen,
            ),
        ):
            MODULE._run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=False,
            )

        environment = captured["env"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["LC_ALL"], "C")
        for name in injected:
            self.assertNotIn(name, environment)

    def test_active_skill_audit_detects_root_replacement_from_bound_fd(
        self,
    ) -> None:
        skills = self.home / "skills"
        original = skills / "original"
        original.mkdir(parents=True)
        (original / "SKILL.md").write_text(
            "---\nname: original\n---\n",
            encoding="utf-8",
        )
        replacement = self.home / "replacement-skills"
        injected = replacement / "injected"
        injected.mkdir(parents=True)
        (injected / "SKILL.md").write_text(
            "---\nname: injected\n---\n",
            encoding="utf-8",
        )
        retained = self.home / "retained-skills"
        real_names = MODULE._bounded_skill_child_names
        swapped = False

        def list_then_swap(directory_fd: int) -> tuple[str, ...]:
            nonlocal swapped
            names = real_names(directory_fd)
            if not swapped:
                swapped = True
                skills.rename(retained)
                replacement.rename(skills)
            return names

        with mock.patch.object(
            MODULE,
            "_bounded_skill_child_names",
            side_effect=list_then_swap,
        ):
            issues = MODULE.audit_active_skills(self.home)

        self.assertTrue(swapped)
        self.assertIn("skills-root-unsafe", {issue.code for issue in issues})
        self.assertFalse(any("injected" in issue.detail for issue in issues))

    def test_uninstall_refuses_scheduler_symlink_without_deleting_target(
        self,
    ) -> None:
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        target = self.root / "outside-service"
        target.write_text("keep\n", encoding="utf-8")
        paths.systemd_service.symlink_to(target)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "non-file sync state",
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=False,
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(paths.systemd_service.is_symlink())

    def test_active_skill_scan_is_bounded_and_requires_closed_frontmatter(
        self,
    ) -> None:
        skills = self.home / "skills"
        skills.mkdir()
        for name in ("one", "two", "three"):
            root = skills / name
            root.mkdir()
            (root / "SKILL.md").write_text(
                f"---\nname: {name}\n---\n",
                encoding="utf-8",
            )
        with mock.patch.object(MODULE, "MAX_ACTIVE_SKILL_ENTRIES", 2):
            issues = MODULE.audit_active_skills(self.home)
        self.assertIn("skills-root-overflow", {issue.code for issue in issues})

        shutil.rmtree(skills)
        malformed = skills / "malformed"
        malformed.mkdir(parents=True)
        (malformed / "SKILL.md").write_text(
            "---\nname: missing-close\n",
            encoding="utf-8",
        )
        issues = MODULE.audit_active_skills(self.home)
        self.assertIn("invalid-frontmatter", {issue.code for issue in issues})

        shutil.rmtree(skills)
        large = skills / "large-body"
        large.mkdir(parents=True)
        (large / "SKILL.md").write_bytes(
            b"---\nname: large-body\n---\n"
            + b"x" * (MODULE.MAX_SKILL_FRONTMATTER_BYTES + 1024)
            + b"\xff"
        )
        issues = MODULE.audit_active_skills(self.home)
        self.assertIn("unmanaged-skill", {issue.code for issue in issues})
        self.assertNotIn("invalid-frontmatter", {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()
