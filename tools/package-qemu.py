#!/usr/bin/env python3
"""Stage a native QEMU release with licenses and its committed source trees."""

import argparse
import configparser
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "qemu/upstream"


def command(*arguments, cwd=None):
    return subprocess.check_output(arguments, cwd=cwd, text=True, encoding="utf-8").strip()


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_identities():
    root_commit = command("git", "rev-parse", "HEAD", cwd=ROOT)
    native_commit = command("git", "rev-parse", "HEAD", cwd=NATIVE)
    gitlink = command("git", "ls-tree", root_commit, "qemu/upstream", cwd=ROOT)
    lock = dict(line.split("=", 1) for line in (ROOT / "qemu.lock").read_text().splitlines()
                if line and not line.startswith("#"))
    if (not gitlink.startswith(f"160000 commit {native_commit}\t") or
            lock.get("fork_commit") != native_commit):
        raise ValueError("native HEAD, root gitlink and qemu.lock must agree")
    # ROM submodule checkouts are not part of the board binary or source archive.
    subprocess.run(["git", "diff", "--quiet", "--ignore-submodules=all", "HEAD"],
                   cwd=ROOT, check=True)
    subprocess.run(["git", "diff", "--quiet", "--ignore-submodules=all", "HEAD"],
                   cwd=NATIVE, check=True)
    return root_commit, native_commit


def archive_source(repository, commit, name, directory):
    path = directory / f"{name}-{commit}.tar.gz"
    subprocess.run(["git", "archive", "--format=tar.gz", f"--prefix={name}-{commit}/",
                    "--output", str(path), commit], cwd=repository, check=True)
    return {"path": f"sources/{path.name}", "commit": commit, "sha256": digest(path)}


def archive_subproject(name, sources, licenses):
    wrap = configparser.ConfigParser()
    wrap.read(NATIVE / "subprojects" / f"{name}.wrap", encoding="utf-8")
    identity = wrap["wrap-git"]
    repository = NATIVE / "subprojects" / name
    revision = identity["revision"]
    if command("git", "rev-parse", "HEAD", cwd=repository) != revision:
        raise ValueError(f"{name} HEAD must match its pinned wrap revision")
    subprocess.run(["git", "diff", "--quiet", "--ignore-submodules=all", "HEAD"],
                   cwd=repository, check=True)
    result = archive_source(repository, revision, name, sources)
    result.update(url=identity["url"], restore_path=f"qemu/upstream/subprojects/{name}")
    license_names = {
        "dtc": ("BSD-2-Clause", "GPL", "README.license"),
        "keycodemapdb": ("LICENSE.GPL2", "LICENSE.BSD"),
    }
    destination = licenses / name
    destination.mkdir()
    for filename in license_names[name]:
        shutil.copy2(repository / filename, destination / filename)
    return result


def windows_dependencies(binary, dll_directory):
    # The pinned upstream installer owns PE dependency traversal; share it rather
    # than maintaining a second objdump parser here.
    spec = importlib.util.spec_from_file_location("qemu_nsis", NATIVE / "scripts/nsis.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dependencies = module.find_deps(str(binary), str(dll_directory), set())
    return sorted({Path(path) for path in dependencies if Path(path) != binary},
                  key=lambda path: path.name.lower())


def msys2_license(owner, destination):
    listing = command("pacman", "-Ql", owner)
    directory = destination / "licenses/msys2" / owner
    directory.mkdir(parents=True)
    copied = []
    for line in listing.splitlines():
        _, path = line.split(" ", 1)
        if "/share/licenses/" not in path or path.endswith("/"):
            continue
        source = Path(command("cygpath", "-w", path))
        relative = path.split("/share/licenses/", 1)[1]
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target.relative_to(destination)).replace("\\", "/"))
    if not copied:
        raise ValueError(f"MSYS2 package has no installed license files: {owner}")
    information = command("pacman", "-Qi", owner)
    (directory / "PACKAGE.txt").write_text(information + "\n", encoding="utf-8")
    return {"package": command("pacman", "-Q", owner), "licenses": copied,
            "package_url": f"https://packages.msys2.org/package/{owner}"}


def package(binary, output, dll_directory=None):
    binary = binary.resolve(strict=True)
    if not binary.is_file():
        raise ValueError(f"QEMU must be a regular file: {binary}")
    if binary.suffix.lower() == ".exe" and dll_directory is None:
        raise ValueError("Windows QEMU requires --dll-dir for its runtime dependencies")
    if dll_directory is not None:
        dll_directory = dll_directory.resolve(strict=True)
        if not dll_directory.is_dir():
            raise ValueError("--dll-dir must be a directory")
    root_commit, native_commit = source_identities()
    output = output.absolute()
    output.mkdir(parents=True)
    (output / "bin").mkdir()
    shutil.copy2(binary, output / "bin" / binary.name)
    (output / "docs").mkdir()
    shutil.copy2(ROOT / "README.md", output / "README.md")
    shutil.copy2(ROOT / "docs/USAGE.md", output / "docs/USAGE.md")
    shutil.copy2(ROOT / "qemu.lock", output / "qemu.lock")
    licenses = output / "licenses"
    licenses.mkdir()
    for name in ("COPYING", "COPYING.LIB"):
        shutil.copy2(NATIVE / name, licenses / name)
    root_license = ROOT / "LICENSE"
    if root_license.is_file():
        shutil.copy2(root_license, licenses / "LICENSE")

    dlls, owners = [], {}
    if dll_directory is not None:
        for dependency in windows_dependencies(binary, dll_directory):
            unix_path = command("cygpath", "-u", str(dependency))
            owner = command("pacman", "-Qoq", unix_path)
            if owner not in owners:
                owners[owner] = msys2_license(owner, output)
            shutil.copy2(dependency, output / "bin" / dependency.name)
            dlls.append({"file": f"bin/{dependency.name}", "sha256": digest(dependency),
                         "package": owner})

    sources = output / "sources"
    sources.mkdir()
    source_archives = [archive_source(ROOT, root_commit, "dm-mc02-qemu", sources),
                       archive_source(NATIVE, native_commit, "qemu", sources)]
    # libfdt is linked statically; generated keycode tables are linked into QEMU.
    # Berkeley softfloat/testfloat subprojects are only used by unpublished tests.
    source_archives.extend(archive_subproject(name, sources, licenses)
                           for name in ("dtc", "keycodemapdb"))
    provenance = {
        "schema": 1,
        "root_commit": root_commit,
        "native_commit": native_commit,
        "qemu": {"file": f"bin/{binary.name}", "sha256": digest(binary)},
        "qemu_lock_sha256": digest(ROOT / "qemu.lock"),
        "root_license": "licenses/LICENSE" if root_license.is_file() else None,
        "dlls": dlls,
        "msys2_packages": owners,
        "sources": source_archives,
        "source_submodules": "Git archives omit submodule checkouts, including ROM repositories.",
    }
    (output / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True, type=Path, help="built native QEMU executable")
    parser.add_argument("--output", required=True, type=Path, help="new release directory")
    parser.add_argument("--dll-dir", type=Path, help="MSYS2 MinGW bin directory, for Windows")
    arguments = parser.parse_args()
    destination = package(arguments.qemu, arguments.output, arguments.dll_dir)
    print(f"Release staged: {destination}")


if __name__ == "__main__":
    main()
