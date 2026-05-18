#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from typing import Any


DEFAULT_MANIFEST = Path("personal_codex/public-sync-manifest.json")
RELEASE_MANIFEST = Path("personal_codex/sync-manifest.json")


class PackageError(RuntimeError):
    pass


def _load_manifest(repo_root: Path, manifest_path: Path) -> dict[str, Any]:
    absolute_path = repo_root / manifest_path
    try:
        data = json.loads(absolute_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PackageError(f"failed to read manifest {manifest_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise PackageError(f"manifest {manifest_path} is invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise PackageError(f"manifest {manifest_path} must be a JSON object")
    return data


def _manifest_sources(manifest: dict[str, Any]) -> list[Path]:
    sources: list[Path] = []
    for section in ("links", "reference_only"):
        items = manifest.get(section, [])
        if not isinstance(items, list):
            raise PackageError(f"manifest {section} must be a list")
        for item in items:
            if section == "links":
                if not isinstance(item, dict):
                    raise PackageError("manifest link entries must be objects")
                source = item.get("source")
            else:
                source = item
            if not isinstance(source, str):
                raise PackageError(f"manifest {section} entry has no string source")
            source_path = PurePosixPath(source)
            if source_path.is_absolute() or ".." in source_path.parts:
                raise PackageError(f"refusing unsafe manifest source: {source}")
            sources.append(Path(*source_path.parts))
    return sources


def _copy_source(repo_root: Path, staging_root: Path, source: Path) -> None:
    source_path = repo_root / source
    destination = staging_root / source
    if not source_path.exists():
        raise PackageError(f"manifest source is missing: {source}")
    if source_path.is_symlink():
        raise PackageError(f"refusing to package symlink source: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source_path.is_dir():
        shutil.copytree(source_path, destination, symlinks=False)
    elif source_path.is_file():
        shutil.copy2(source_path, destination)
    else:
        raise PackageError(f"unsupported manifest source type: {source}")


def stage_release(repo_root: Path, manifest_path: Path, staging_root: Path) -> None:
    manifest = _load_manifest(repo_root, manifest_path)
    for source in _manifest_sources(manifest):
        if source == RELEASE_MANIFEST:
            continue
        _copy_source(repo_root, staging_root, source)
    release_manifest_path = staging_root / RELEASE_MANIFEST
    release_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    release_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _iter_tar_paths(root: Path) -> list[Path]:
    paths = [path for path in root.rglob("*") if path.name != ".DS_Store"]
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = ""
    tarinfo.gname = ""
    tarinfo.mtime = 0
    if tarinfo.isdir():
        tarinfo.mode = 0o755
    elif tarinfo.mode & 0o111:
        tarinfo.mode = 0o755
    else:
        tarinfo.mode = 0o644
    return tarinfo


def create_archive(staging_root: Path, archive_path: Path, package_name: str) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as raw_file:
        with gzip.GzipFile(fileobj=raw_file, mode="wb", mtime=0) as gzip_file:
            with tarfile.open(fileobj=gzip_file, mode="w") as archive:
                for path in _iter_tar_paths(staging_root):
                    relative_path = path.relative_to(staging_root).as_posix()
                    archive.add(
                        path,
                        arcname=f"{package_name}/{relative_path}",
                        recursive=False,
                        filter=_tar_filter,
                    )


def write_checksum(archive_path: Path) -> Path:
    digest = hashlib.sha256()
    with archive_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum_path = archive_path.parent / archive_path.name.removesuffix(".tar.gz")
    checksum_path = checksum_path.with_suffix(".sha256")
    checksum_path.write_text(
        f"{digest.hexdigest()}  {archive_path.name}\n",
        encoding="utf-8",
    )
    return checksum_path


def build_package(repo_root: Path, manifest_path: Path, output_dir: Path, sha: str) -> tuple[Path, Path]:
    package_name = f"personal-codex-{sha}"
    archive_path = output_dir / f"{package_name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="personal-codex-package.") as temp_dir_raw:
        staging_root = Path(temp_dir_raw) / package_name
        staging_root.mkdir(parents=True)
        stage_release(repo_root, manifest_path, staging_root)
        create_archive(staging_root, archive_path, package_name)
    checksum_path = write_checksum(archive_path)
    return archive_path, checksum_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a personal Codex release package.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument("--sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest)
    output_dir = Path(args.output_dir)
    archive_path, checksum_path = build_package(repo_root, manifest_path, output_dir, args.sha)
    print(archive_path)
    print(checksum_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
