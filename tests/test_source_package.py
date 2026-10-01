"""Failure boundaries for the QEMU source snapshot format."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tarfile
import subprocess

import pytest


TOOL = Path(__file__).resolve().parents[1] / "tools/dm_mc02_source_package.py"
SPEC = importlib.util.spec_from_file_location("source_package", TOOL)
assert SPEC is not None and SPEC.loader is not None
source_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source_package)


def archive_with_file(path: Path, name: str = "project/input.txt",
                      data: bytes = b"source", declared: bytes | None = None) -> None:
    declared = data if declared is None else declared
    manifest = {"files": [{"path": name, "kind": "file", "mode": 0o644,
                           "size": len(declared),
                           "sha256": source_package.sha256(declared)}]}
    with tarfile.open(path, "w:gz") as archive:
        source = tarfile.TarInfo(name)
        source.size = len(data)
        source.mode = 0o644
        archive.addfile(source, io.BytesIO(data))
        encoded = json.dumps(manifest).encode()
        record = tarfile.TarInfo("manifest.json")
        record.size = len(encoded)
        archive.addfile(record, io.BytesIO(encoded))


def test_restore_checks_bytes_and_destination(tmp_path: Path) -> None:
    package = tmp_path / "valid.tar.gz"
    archive_with_file(package)
    destination = tmp_path / "restore"
    source_package.restore(package, destination)
    assert (destination / "project/input.txt").read_bytes() == b"source"
    with pytest.raises(ValueError, match="existing destination"):
        source_package.restore(package, destination)


def test_verify_rejects_modified_bytes(tmp_path: Path) -> None:
    package = tmp_path / "modified.tar.gz"
    archive_with_file(package, data=b"change", declared=b"source")
    with pytest.raises(ValueError, match="source mismatch"):
        source_package.verify(package)


def test_verify_rejects_path_traversal(tmp_path: Path) -> None:
    package = tmp_path / "traversal.tar.gz"
    archive_with_file(package, name="project/../outside")
    with pytest.raises(ValueError, match="unsafe archive path"):
        source_package.verify(package)


def test_wrap_overlays_are_build_inputs() -> None:
    qemu_paths = [
        "subprojects/packagefiles/berkeley-softfloat-3/meson.build",
        "subprojects/packagefiles/berkeley-softfloat-3/meson_options.txt",
        "subprojects/packagefiles/berkeley-testfloat-3/meson.build",
        "subprojects/packagefiles/berkeley-testfloat-3/meson_options.txt",
    ]
    for wrap in ("berkeley-softfloat-3", "berkeley-testfloat-3"):
        assert source_package.wrap_overlay_paths(wrap, qemu_paths) == [
            "meson.build", "meson_options.txt"
        ]


def test_project_inventory_excludes_gitlink_not_source(monkeypatch, tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "README.md").write_text("project")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "update-index", "--add",
                    "--cacheinfo", "160000," + "1" * 40 + ",qemu/upstream"], check=True)
    (tmp_path / "new-tool.py").write_text("new source")
    monkeypatch.setattr(source_package, "ROOT", tmp_path)
    assert source_package.project_paths() == ["README.md", "new-tool.py"]


def test_fork_base_snapshot_matches_pinned_upstream_tree(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "qemu.c").write_text("official source")
    git("add", "qemu.c")
    git("commit", "-m", "upstream")
    upstream = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    base = git("commit-tree", tree, "-m", "source snapshot")
    fork = git("commit-tree", tree, "-p", base, "-m", "project changes")
    lock = {"upstream_commit": upstream, "upstream_tree": tree,
            "fork_base_commit": base}

    assert source_package.validate_qemu_fork(tmp_path, lock, fork) == (base, tree)
    with pytest.raises(ValueError, match="upstream_tree"):
        source_package.validate_qemu_fork(tmp_path, {**lock, "upstream_tree": "0" * 40}, fork)
    with pytest.raises(ValueError, match="pinned base commit"):
        source_package.validate_qemu_fork(tmp_path, {**lock, "fork_base_commit": upstream}, fork)


def test_worktree_snapshot_preserves_edits_additions_and_deletions(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True)

    git("init")
    (tmp_path / "changed.c").write_text("old")
    (tmp_path / "removed.c").write_text("old")
    (tmp_path / ".gitignore").write_text("build/\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "fixture")
    (tmp_path / "changed.c").write_text("new")
    (tmp_path / "removed.c").unlink()
    (tmp_path / "new.c").write_text("new device")
    (tmp_path / "build").mkdir()
    (tmp_path / "build/generated").write_text("ignored")
    with pytest.raises(ValueError, match="unrecorded changes"):
        source_package.qemu_snapshot(tmp_path, False)
    paths, meta = source_package.qemu_snapshot(tmp_path, True)
    assert paths == [".gitignore", "changed.c", "new.c"]
    assert meta["mode"] == "worktree"
    assert meta["deleted_paths"] == ["removed.c"]
    assert meta["untracked_paths"] == ["new.c"]
    package = tmp_path / "snapshot.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        records = [source_package.add_source(archive, "project/" + p, tmp_path / p)
                   for p in paths]
        data = json.dumps({"files": records, "qemu_worktree": meta}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    destination = tmp_path / "restored"
    source_package.restore(package, destination)
    assert (destination / "project/changed.c").read_text() == "new"
    assert (destination / "project/new.c").read_text() == "new device"
    assert not (destination / "project/removed.c").exists()
