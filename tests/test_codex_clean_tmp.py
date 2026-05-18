from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "personal_codex" / "bin" / "codex-clean-tmp"


class CodexCleanTmpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="codex-clean-tmp.")
        self.repo_root = Path(self.tmpdir.name) / "repo"
        self.repo_root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo_root, check=True)
        self.scratch_root = self.repo_root / ".codex-tmp"
        self.scratch_root.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT_PATH), *args],
            cwd=self.repo_root,
            check=False,
            text=True,
            capture_output=True,
        )

    def replace_scratch_root_with_symlink(self, target: Path) -> None:
        shutil.rmtree(self.scratch_root)
        self.scratch_root.symlink_to(target, target_is_directory=True)

    def test_removes_relative_subpath(self) -> None:
        target = self.scratch_root / "example" / "nested.txt"
        target.parent.mkdir(parents=True)
        target.write_text("payload", encoding="utf-8")

        result = self.run_helper("example")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.scratch_root / "example").exists())
        self.assertIn("Removed:", result.stdout)

    def test_accepts_repo_relative_dot_codex_tmp_path(self) -> None:
        target = self.scratch_root / "scoped" / "item.txt"
        target.parent.mkdir(parents=True)
        target.write_text("payload", encoding="utf-8")

        result = self.run_helper(".codex-tmp/scoped/item.txt")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())

    def test_rejects_parent_traversal(self) -> None:
        outside = self.repo_root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        result = self.run_helper("../outside.txt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Parent traversal is not allowed", result.stderr)
        self.assertTrue(outside.exists())

    def test_rejects_absolute_paths(self) -> None:
        result = self.run_helper("/tmp/not-allowed")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Absolute paths are not allowed", result.stderr)

    def test_rejects_symlinked_scratch_root_for_all(self) -> None:
        outside_dir = Path(self.tmpdir.name) / "outside-all"
        outside_dir.mkdir()
        outside_file = outside_dir / "keep.txt"
        outside_file.write_text("keep", encoding="utf-8")
        self.replace_scratch_root_with_symlink(outside_dir)

        result = self.run_helper("--all")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to operate on symlinked scratch root", result.stderr)
        self.assertTrue(self.scratch_root.is_symlink())
        self.assertTrue(outside_file.exists())

    def test_rejects_symlinked_scratch_root_for_subpath(self) -> None:
        outside_dir = Path(self.tmpdir.name) / "outside-subpath"
        nested_dir = outside_dir / "nested"
        nested_dir.mkdir(parents=True)
        outside_file = nested_dir / "keep.txt"
        outside_file.write_text("keep", encoding="utf-8")
        self.replace_scratch_root_with_symlink(outside_dir)

        result = self.run_helper("nested/keep.txt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to operate on symlinked scratch root", result.stderr)
        self.assertTrue(self.scratch_root.is_symlink())
        self.assertTrue(outside_file.exists())

    def test_rejects_symlinked_ancestor_escape(self) -> None:
        outside_dir = Path(self.tmpdir.name) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        (self.scratch_root / "linkdir").symlink_to(outside_dir, target_is_directory=True)

        result = self.run_helper("linkdir/secret.txt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to traverse symlinked ancestor", result.stderr)
        self.assertTrue(outside_file.exists())

    def test_removes_symlink_leaf_without_following_it(self) -> None:
        outside_file = Path(self.tmpdir.name) / "outside.txt"
        outside_file.write_text("payload", encoding="utf-8")
        link = self.scratch_root / "leaf-link"
        link.symlink_to(outside_file)

        result = self.run_helper("leaf-link")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.exists())
        self.assertTrue(outside_file.exists())

    def test_all_removes_scratch_root(self) -> None:
        target = self.scratch_root / "example"
        target.mkdir()

        result = self.run_helper("--all")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.scratch_root.exists())

    def test_rejects_trailing_slash_scratch_root_without_all(self) -> None:
        target = self.scratch_root / "example"
        target.mkdir()

        result = self.run_helper(".codex-tmp/")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to remove the scratch root without --all", result.stderr)
        self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
