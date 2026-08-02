from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPOSITORY_ROOT / "scripts" / "run_ci_tests_with_private_tmp.sh"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"


class CIPrivateTempTests(unittest.TestCase):
    def setUp(self) -> None:
        account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve()
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix=".ci-private-tmp-tests.",
            dir=account_home,
        )
        self.root = Path(os.path.realpath(self.temporary_directory.name))
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run(self, probe: Path, *arguments: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["HOME"] = self.home.as_posix()
        return subprocess.run(
            [
                "bash",
                RUNNER.as_posix(),
                "--",
                sys.executable,
                probe.as_posix(),
                *(argument.as_posix() for argument in arguments),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_workflows_route_linux_suites_through_runner(self) -> None:
        ci_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        release_workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        discovery_launch = (
            "bash scripts/run_ci_tests_with_private_tmp.sh --\n"
            "          python3 -m unittest discover -s tests"
        )
        platform_launch = (
            "bash scripts/run_ci_tests_with_private_tmp.sh -- \\\n"
            '              python3 -m unittest "${modules[@]}"'
        )

        self.assertIn("runs-on: ubuntu-latest", ci_workflow)
        self.assertIn(discovery_launch, ci_workflow)
        self.assertIn('if [[ "$RUNNER_OS" == "Linux" ]]; then', ci_workflow)
        self.assertIn(platform_launch, ci_workflow)
        self.assertIn(
            'else\n            python3 -m unittest "${modules[@]}"',
            ci_workflow,
        )
        self.assertIn("runs-on: ubuntu-latest", release_workflow)
        self.assertIn(discovery_launch, release_workflow)
        self.assertNotIn("export TMPDIR", ci_workflow)
        self.assertNotIn("export TMPDIR", release_workflow)

    def test_runner_scopes_private_mode_0700_tmpdir_to_child_and_cleans_it(
        self,
    ) -> None:
        probe = self.root / "probe.py"
        result_path = self.root / "probe-result.json"
        probe.write_text(
            textwrap.dedent(
                """\
                import json
                import os
                from pathlib import Path
                import stat
                import sys

                temporary = Path(os.environ["TMPDIR"])
                metadata = temporary.stat()
                Path(sys.argv[1]).write_text(
                    json.dumps(
                        {
                            "path": temporary.as_posix(),
                            "parent": temporary.parent.as_posix(),
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "uid": metadata.st_uid,
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ),
            encoding="utf-8",
        )

        completed = self._run(probe, result_path)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["parent"], self.home.as_posix())
        self.assertEqual(result["mode"], 0o700)
        self.assertEqual(result["uid"], os.getuid())
        self.assertFalse(Path(result["path"]).exists())

    def test_runner_retains_and_reports_a_replacement(self) -> None:
        probe = self.root / "replace.py"
        result_path = self.root / "replacement-result.json"
        probe.write_text(
            textwrap.dedent(
                """\
                import json
                import os
                from pathlib import Path
                import sys

                temporary = Path(os.environ["TMPDIR"])
                original = temporary.with_name(f"{temporary.name}.original")
                temporary.rename(original)
                temporary.mkdir(mode=0o700)
                Path(sys.argv[1]).write_text(
                    json.dumps(
                        {
                            "original": original.as_posix(),
                            "replacement": temporary.as_posix(),
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ),
            encoding="utf-8",
        )

        completed = self._run(probe, result_path)

        self.assertEqual(completed.returncode, 75, completed.stderr)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertTrue(Path(result["original"]).is_dir())
        self.assertTrue(Path(result["replacement"]).is_dir())
        self.assertEqual(list(Path(result["replacement"]).iterdir()), [])
        self.assertIn("retained replacement at CI temp root", completed.stderr)

    def test_runner_rejects_a_writable_home_ancestor_before_allocation(self) -> None:
        unsafe_parent = self.root / "unsafe"
        unsafe_parent.mkdir(mode=0o777)
        unsafe_parent.chmod(0o777)
        unsafe_home = unsafe_parent / "home"
        unsafe_home.mkdir(mode=0o700)
        probe = self.root / "should-not-run.py"
        probe.write_text(
            "raise SystemExit('probe unexpectedly ran')\n", encoding="utf-8"
        )
        environment = dict(os.environ)
        environment["HOME"] = unsafe_home.as_posix()

        completed = subprocess.run(
            ["bash", RUNNER.as_posix(), "--", sys.executable, probe.as_posix()],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("trusted HOME ancestor is group/world writable", completed.stderr)
        self.assertEqual(list(unsafe_home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
