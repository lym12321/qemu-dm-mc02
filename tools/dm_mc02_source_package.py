#!/usr/bin/env python3
"""Create and verify a source snapshot for an independent QEMU rebuild."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
QEMU = ROOT / "qemu/upstream"
WRAP_GIT = ("dtc", "keycodemapdb", "berkeley-softfloat-3", "berkeley-testfloat-3")
ELF = Path(os.environ.get("DM_MC02_ELF", ROOT.parent / "trobot/build/Release-current/trobot.elf"))


def git(directory: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(directory), *args])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_paths(directory: Path) -> list[str]:
    entries = git(directory, "ls-files", "--stage", "-z").split(b"\0")
    paths = []
    for entry in entries:
        if not entry:
            continue
        meta, raw_path = entry.split(b"\t", 1)
        if meta.split(b" ", 1)[0] != b"160000":
            paths.append(os.fsdecode(raw_path))
    return paths


def project_paths() -> list[str]:
    raw = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    paths = sorted({os.fsdecode(path) for path in raw.split(b"\0") if path})
    if any(path.startswith("qemu/upstream/") for path in paths):
        raise ValueError("outer project must not own nested QEMU source")
    return [path for path in paths if path != "qemu/upstream"]


def clean_source(directory: Path) -> None:
    status = git(directory, "status", "--porcelain", "--untracked-files=no",
                 "--ignore-submodules=all")
    if status:
        raise ValueError(f"source has unrecorded changes: {directory}: {status[:400]!r}")


def qemu_snapshot(directory: Path, allow_worktree: bool) -> tuple[list[str], dict]:
    """Select source bytes, never submodule worktrees, for an explicit snapshot."""
    tracked = set(tracked_paths(directory))
    untracked = [os.fsdecode(p) for p in
                 git(directory, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
    if not allow_worktree:
        clean_source(directory)
        if untracked:
            raise ValueError("nested QEMU has unbundled source")
    # Missing tracked files are intentional deletions in worktree mode.
    paths = sorted(p for p in tracked | set(untracked)
                   if (directory / p).exists() or (directory / p).is_symlink())
    delta = git(directory, "diff", "--binary", "--ignore-submodules=all", "HEAD", "--")
    return paths, {
        "mode": "worktree" if allow_worktree else "committed",
        "sha256_tracked_diff": sha256(delta),
        "untracked_paths": sorted(untracked),
        "deleted_paths": sorted(tracked - set(paths)),
        "note": "HEAD is a base identity; files manifest identifies actual delivered bytes",
    }


def validate_qemu_fork(directory: Path, lock: dict, qemu_head: str) -> tuple[str, str]:
    upstream_commit = lock["upstream_commit"]
    fork_base = lock.get("fork_base_commit", upstream_commit)
    try:
        merge_base = git(directory, "merge-base", qemu_head, fork_base).decode().strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError("QEMU fork is missing its pinned base commit") from exc
    if merge_base != fork_base:
        raise ValueError("QEMU fork does not descend from qemu.lock fork_base_commit")

    base_tree = git(directory, "rev-parse", f"{fork_base}^{{tree}}").decode().strip()
    expected_tree = lock.get("upstream_tree")
    if expected_tree:
        if base_tree != expected_tree:
            raise ValueError("QEMU fork base tree does not match qemu.lock upstream_tree")
        try:
            source_tree = git(directory, "rev-parse", f"{upstream_commit}^{{tree}}").decode().strip()
        except subprocess.CalledProcessError:
            source_tree = None
        if source_tree and source_tree != expected_tree:
            raise ValueError("qemu.lock upstream_commit does not match upstream_tree")
    else:
        try:
            base_tree = git(directory, "rev-parse", f"{upstream_commit}^{{tree}}").decode().strip()
        except subprocess.CalledProcessError as exc:
            raise ValueError("qemu.lock lacks upstream_tree for a snapshot base") from exc
    return fork_base, base_tree


def wrap_revision(name: str) -> str:
    parser = configparser.ConfigParser()
    parser.read(QEMU / "subprojects" / f"{name}.wrap")
    return parser["wrap-git"]["revision"]


def wrap_overlay_paths(name: str, qemu_paths: list[str]) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(QEMU / "subprojects" / f"{name}.wrap")
    patch_directory = parser["wrap-git"].get("patch_directory")
    if not patch_directory:
        return []
    prefix = f"subprojects/packagefiles/{patch_directory}/"
    overlays = sorted(path[len(prefix):] for path in qemu_paths
                      if path.startswith(prefix))
    if not overlays:
        raise ValueError(f"missing QEMU wrap overlay: {name}")
    return overlays


def version(*command: str) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode:
        return f"unavailable (exit {result.returncode})"
    return (result.stdout or result.stderr).splitlines()[0].strip()


def sources(allow_worktree: bool = False) -> tuple[list[tuple[str, Path]], dict]:
    qemu_paths, qemu_worktree = qemu_snapshot(QEMU, allow_worktree)
    qemu_head = git(QEMU, "rev-parse", "HEAD").decode().strip()
    lock = dict(line.split("=", 1) for line in
                (ROOT / "qemu.lock").read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#") and "=" in line)
    if lock.get("fork_commit") != qemu_head:
        raise ValueError("qemu.lock fork_commit does not match nested QEMU HEAD")
    upstream_base, base_tree = validate_qemu_fork(QEMU, lock, qemu_head)
    expected_tree = lock.get("upstream_tree", base_tree)

    paths = [(f"project/{path}", ROOT / path) for path in project_paths()
             if (ROOT / path).exists() or (ROOT / path).is_symlink()]
    paths += [(f"project/qemu/upstream/{path}", QEMU / path)
              for path in qemu_paths]
    outer_changes = git(ROOT, "diff", "--binary", "HEAD", "--")
    qemu_delta = git(QEMU, "diff", "--binary", "--ignore-submodules=all",
                     upstream_base, "--")

    wraps = {}
    for name in WRAP_GIT:
        directory = QEMU / "subprojects" / name
        clean_source(directory)
        actual = git(directory, "rev-parse", "HEAD").decode().strip()
        expected = wrap_revision(name)
        if actual != expected:
            raise ValueError(f"{name} HEAD {actual} differs from wrap {expected}")
        parser = configparser.ConfigParser()
        parser.read(QEMU / "subprojects" / f"{name}.wrap")
        wraps[name] = {"revision": actual, "url": parser["wrap-git"]["url"]}
        paths += [(f"project/qemu/upstream/subprojects/{name}/{path}", directory / path)
                  for path in tracked_paths(directory)]
        overlays = wrap_overlay_paths(name, qemu_paths)
        paths += [(f"project/qemu/upstream/subprojects/{name}/{overlay}",
                   QEMU / f"subprojects/packagefiles/{name}/{overlay}")
                  for overlay in overlays]
        if overlays:
            wraps[name]["overlay_paths"] = overlays

    if not ELF.is_file():
        raise ValueError(f"read-only firmware input is missing: {ELF}")
    observed_dependencies = ROOT / "build/qemu/meson-info/intro-dependencies.json"
    dependencies = []
    if observed_dependencies.is_file():
        dependencies = [{key: item[key] for key in ("name", "version", "type")}
                        for item in json.loads(observed_dependencies.read_text())]
    metadata = {
        "schema": 2,
        "outer_head": git(ROOT, "rev-parse", "HEAD").decode().strip(),
        "qemu_head": qemu_head,
        "qemu_worktree": qemu_worktree,
        "upstream_base": upstream_base,
        "upstream_url": lock["upstream_url"],
        "upstream_source_commit": lock["upstream_commit"],
        "upstream_source_tree": expected_tree or base_tree,
        "qemu_local_delta": {"source": "upstream_base..tracked_worktree (excluding submodules)",
                             "sha256_binary_diff": sha256(qemu_delta),
                             "changed_paths": len(git(QEMU, "diff", "--name-only",
                                                      "--ignore-submodules=all",
                                                      upstream_base, "--").splitlines()),
                             "note": "full fork source is bundled; no second patch owner"},
        "outer_worktree": {"sha256_tracked_diff": sha256(outer_changes),
                           "untracked_paths": [path for path in project_paths()
                                               if path not in set(tracked_paths(ROOT))]},
        "wrap_git": wraps,
        "observed_qemu_dependencies": dependencies,
        "roms_submodules": "QEMU gitlinks retained in source metadata; ROM submodule bytes are not needed by the selected ARM project gate and are not bundled",
        "firmware": {"path_hint": ELF.name,
                     "size": ELF.stat().st_size, "sha256": file_sha256(ELF),
                     "bundled": False},
        "build_profile": {
            "qemu": "tools/build-qemu.sh: arm-softmmu, dm-mc02, release, internal fdt, no slirp/capstone/docs/werror",
            "host": "cmake -S . -B build/host -DCMAKE_BUILD_TYPE=Release",
            "test": "python3 tools/dm_mc02_test_gate.py --no-build --jobs 4",
            "python": "uv sync --locked --group dev --extra mujoco",
        },
        "tools": {"python": version(sys.executable, "--version"),
                  "cc": version("cc", "--version"),
                  "cmake": version("cmake", "--version"),
                  "uv": version("uv", "--version"),
                  "arm-none-eabi-gcc": version("arm-none-eabi-gcc", "--version"),
                  "meson": version(str(ROOT / "tools/meson"), "--version"),
                  "ninja": version("ninja", "--version")},
    }
    return sorted(paths), metadata


def add_source(archive: tarfile.TarFile, name: str, path: Path) -> dict:
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = path.lstat().st_mode & 0o777
    if path.is_symlink():
        target = os.readlink(path)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        archive.addfile(info)
        data = os.fsencode(target)
        kind = "symlink"
    elif path.is_file():
        data = path.read_bytes()
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        kind = "file"
    else:
        raise ValueError(f"unsupported source type: {path}")
    return {"path": name, "kind": kind, "mode": info.mode,
            "size": len(data), "sha256": sha256(data)}


def create(output: Path, allow_worktree: bool = False) -> None:
    if output.exists():
        raise ValueError(f"refusing to overwrite package: {output}")
    paths, metadata = sources(allow_worktree)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tar.gz", delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            metadata["files"] = [add_source(archive, name, path) for name, path in paths]
            content = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
        verify(temporary)
        # Reject edits/deletions/additions while the archive was being made.
        final_paths, final_metadata = sources(allow_worktree)
        if paths != final_paths or metadata["qemu_worktree"] != final_metadata["qemu_worktree"]:
            raise ValueError("source changed during packaging")
        for item, (_, path) in zip(metadata["files"], paths):
            data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            if (sha256(data) != item["sha256"] or
                    path.lstat().st_mode & 0o777 != item["mode"]):
                raise ValueError(f"source changed during packaging: {path}")
        temporary.rename(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"created {output} ({len(paths)} source paths, sha256 {file_sha256(output)})")


def checked_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not name.startswith("project/"):
        raise ValueError(f"unsafe archive path: {name}")


def verify(package: Path) -> dict:
    with tarfile.open(package, "r:gz") as archive:
        members = archive.getmembers()
        if not members or members[-1].name != "manifest.json":
            raise ValueError("manifest must be the final archive member")
        manifest_file = archive.extractfile(members[-1])
        if manifest_file is None:
            raise ValueError("manifest is not a file")
        manifest = json.load(manifest_file)
        expected = {item["path"]: item for item in manifest["files"]}
        if len(expected) != len(manifest["files"]) or len(members) != len(expected) + 1:
            raise ValueError("duplicate or missing archive path")
        for member in members[:-1]:
            checked_name(member.name)
            item = expected.pop(member.name, None)
            if item is None:
                raise ValueError(f"unexpected archive path: {member.name}")
            if member.issym():
                target = member.linkname
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), target))
                if target.startswith("/") or not resolved.startswith("project/"):
                    raise ValueError(f"unsafe symlink: {member.name} -> {target}")
                data = os.fsencode(target)
                kind = "symlink"
            elif member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable file: {member.name}")
                data = stream.read()
                kind = "file"
            else:
                raise ValueError(f"unsupported archive member: {member.name}")
            if (kind != item["kind"] or len(data) != item["size"] or
                    sha256(data) != item["sha256"] or member.mode != item["mode"]):
                raise ValueError(f"source mismatch: {member.name}")
        if expected:
            raise ValueError(f"missing source path: {next(iter(expected))}")
    return manifest


def verify_tree(manifest: dict, destination: Path) -> None:
    for item in manifest["files"]:
        path = destination / item["path"]
        if item["kind"] == "symlink":
            if not path.is_symlink():
                raise ValueError(f"restored symlink missing: {path}")
            data = os.fsencode(os.readlink(path))
        elif path.is_file() and not path.is_symlink():
            data = path.read_bytes()
        else:
            raise ValueError(f"restored file missing: {path}")
        if len(data) != item["size"] or sha256(data) != item["sha256"]:
            raise ValueError(f"restored content mismatch: {path}")
        if not path.is_symlink() and (path.stat().st_mode & 0o777) != item["mode"]:
            raise ValueError(f"restored mode mismatch: {path}")


def restore(package: Path, destination: Path) -> None:
    manifest = verify(package)
    if destination.exists():
        raise ValueError(f"refusing to use existing destination: {destination}")
    destination.mkdir(parents=True)
    try:
        with tarfile.open(package, "r:gz") as archive:
            for member in archive.getmembers()[:-1]:
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    target.symlink_to(member.linkname)
                else:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError(f"unreadable file: {member.name}")
                    with target.open("wb") as output:
                        shutil.copyfileobj(stream, output)
                    target.chmod(member.mode)
        verify_tree(manifest, destination)
    except Exception:
        shutil.rmtree(destination)
        raise
    print(f"restored {len(manifest['files'])} source paths to {destination / 'project'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("create")
    build.add_argument("package", type=Path)
    build.add_argument("--allow-worktree", action="store_true",
                       help="include uncommitted QEMU sources with explicit manifest identity")
    check = subparsers.add_parser("verify")
    check.add_argument("package", type=Path)
    unpack = subparsers.add_parser("restore")
    unpack.add_argument("package", type=Path)
    unpack.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            create(args.package, args.allow_worktree)
        elif args.command == "verify":
            manifest = verify(args.package)
            print(f"verified {len(manifest['files'])} source paths in {args.package}")
        else:
            restore(args.package, args.destination)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError,
            json.JSONDecodeError, KeyError) as exc:
        print(f"source package error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
