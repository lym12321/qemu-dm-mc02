"""Reject unusable zlib development files before QEMU configuration."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "build-qemu.sh"


def _selected_command(variable: str, fallback: str) -> str:
    words = shlex.split(os.environ.get(variable) or fallback)
    executable = shutil.which(words[0])
    assert executable is not None, f"Missing test tool: {words[0]}"
    return shlex.join([executable, *words[1:]])


TEST_CC = _selected_command("CC", "cc")
TEST_CXX = _selected_command("CXX", "c++")
TEST_PYTHON = _selected_command("PYTHON", sys._base_executable)
TEST_PKG_CONFIG = _selected_command("PKG_CONFIG", "pkg-config")
TEST_BASH = shutil.which("bash")
assert TEST_BASH is not None, "Missing test tool: bash"


def _real_zlib_flags() -> str:
    return subprocess.check_output(
        [*shlex.split(TEST_PKG_CONFIG), "--cflags", "--libs", "zlib"], text=True,
    )


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{TEST_BASH}\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _project(
    tmp_path: Path, zlib_flags: str, *, script_name: str = "build-qemu.sh",
    configure_body: str = "touch configure-entered\nexit 42\n",
) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    (root / "tools").mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT.parent / script_name, root / "tools" / script_name)
    shutil.copy2(BUILD_SCRIPT.parent / "qemu-build-env.sh", root / "tools/qemu-build-env.sh")
    _write_executable(root / "qemu/upstream/configure", configure_body)
    arm = root / "qemu/upstream/hw/arm"
    arm.mkdir(parents=True)
    (arm / "meson.build").write_text("# dm_mc02.c\n", encoding="utf-8")
    (arm / "dm_mc02.c").touch()
    bin_dir = tmp_path / "bin"
    _write_executable(
        bin_dir / "pkg-config",
        'case "$*" in\n'
        '  "--atleast-version=2.56 glib-2.0"|"--exists zlib") exit 0 ;;\n'
        f'  "--cflags --libs zlib") printf "%s\\n" {shlex.quote(zlib_flags)} ;;\n'
        '  *) exit 1 ;;\nesac\n',
    )
    environment = dict(os.environ)
    # Keep the active Nix dependency flags, but isolate configure fixtures from
    # unrelated caller overrides. The real selected tools are explicit below.
    for name in (
        "AR", "AS", "CCAS", "DLLTOOL", "LD", "NM", "OBJCFLAGS", "OBJCOPY",
        "RANLIB", "SDL2_CONFIG", "SMBD", "STRIP", "WIDL", "WINDRES", "WINDMC",
        "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR", "QEMU_RECONFIGURE", "QEMU_QOM_CAST_DEBUG",
    ):
        environment.pop(name, None)
    environment.update({
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "PYTHON": TEST_PYTHON,
        "CC": TEST_CC,
        "CXX": TEST_CXX,
        "PKG_CONFIG": str(bin_dir / "pkg-config"),
        "CFLAGS": "",
        "CXXFLAGS": "",
        "LDFLAGS": "",
        "QEMU_BUILD_TYPE": "release",
        "QEMU_TCG_PLUGINS": "false",
    })
    return root, environment


def _run(root: Path, environment: dict[str, str], script_name: str = "build-qemu.sh"):
    return subprocess.run(
        [TEST_BASH, str(root / "tools" / script_name)],
        env=environment, text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize("failure", ["header", "link"])
@pytest.mark.parametrize("script_name", ["build-qemu.sh", "build-qemu-generic.sh"])
def test_metadata_does_not_admit_unusable_zlib(tmp_path: Path, failure: str, script_name: str) -> None:
    if failure == "header":
        flags = "-lz"
    else:
        headers = tmp_path / "include dir"
        headers.mkdir()
        (headers / "zlib.h").write_text("const char *zlibVersion(void);\n", encoding="utf-8")
        flags = shlex.join(["-I" + str(headers), "-ldm_qemu_missing_zlib_fixture"])
    root, environment = _project(tmp_path, flags, script_name=script_name)
    # Exercise the compiler selected by the caller, with no system headers.
    environment["CC"] += " -nostdinc"
    # Nix wrappers otherwise add the shell's zlib headers explicitly even with
    # -nostdinc, defeating the deliberately missing-header fixture.
    for name in tuple(environment):
        if name.startswith("NIX_CFLAGS_COMPILE"):
            environment[name] = ""

    result = _run(root, environment, script_name)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "blocked: zlib headers or library are unusable" in result.stderr
    assert "apt: zlib1g-dev" in result.stderr
    diagnostic = "zlib.h" if failure == "header" else "dm_qemu_missing_zlib_fixture"
    assert diagnostic in result.stderr
    assert not (root / "build").exists()


@pytest.mark.parametrize("script_name", ["build-qemu.sh", "build-qemu-generic.sh"])
def test_real_zlib_allows_configuration(tmp_path: Path, script_name: str) -> None:
    root, environment = _project(tmp_path, _real_zlib_flags(), script_name=script_name)

    result = _run(root, environment, script_name)

    # The configure fixture deliberately exits; admission must reach it.
    assert result.returncode == 42, result.stdout + result.stderr
    profile = "qemu-generic" if script_name == "build-qemu-generic.sh" else "qemu"
    assert (root / "build" / profile / "configure-entered").exists()
    assert "blocked:" not in result.stderr


def test_default_toolchain_ignores_nix_and_venv_path(tmp_path: Path) -> None:
    # Exercise only tool selection here. Requiring Ubuntu packages to prove
    # the fallback paths would make the Nix test gate depend on apt packages.
    # Real compiler/library admission is covered separately for both profiles.
    environment = dict(os.environ)
    poisoned_bin = tmp_path / ".nix-profile/bin"
    invoked = tmp_path / "wrong-tool-invoked"
    for name in ("cc", "c++", "pkg-config", "python3", "ninja", "nm", "ar", "meson"):
        _write_executable(poisoned_bin / name, f"touch {shlex.quote(str(invoked))}\nexit 91\n")
    for name in ("CC", "CXX", "PKG_CONFIG", "PYTHON"):
        environment.pop(name, None)
    environment.pop("IN_NIX_SHELL", None)
    environment["PATH"] = str(poisoned_bin) + os.pathsep + os.environ["PATH"]

    result = subprocess.run(
        [TEST_BASH, "-c", (
            'set -eu; source "$1"; '
            'printf "%s\\n" "CC=$CC" "CXX=$CXX" "PKG_CONFIG=$PKG_CONFIG" '
            '"PYTHON=$PYTHON" "PATH=$PATH"'
        ), "fixture", str(BUILD_SCRIPT.parent / "qemu-build-env.sh")],
        env=environment, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "CC=/usr/bin/cc", "CXX=/usr/bin/c++", "PKG_CONFIG=/usr/bin/pkg-config",
        "PYTHON=/usr/bin/python3", "PATH=/usr/bin:/bin",
    ]
    assert not invoked.exists()


@pytest.mark.parametrize("script_name", ["build-qemu.sh", "build-qemu-generic.sh"])
def test_nix_shell_preserves_dependency_environment(tmp_path: Path, script_name: str) -> None:
    root, environment = _project(
        tmp_path, _real_zlib_flags(), script_name=script_name,
        configure_body=(
            'printf "%s\\n" "CC=$CC" "CXX=$CXX" "PKG_CONFIG=$PKG_CONFIG" '
            '"PYTHON=$PYTHON" "PATH=$PATH" > configure-environment\nexit 42\n'
        ),
    )
    # Select compiler and Python through PATH, as a shell does by default. The
    # wrappers delegate to the actual pytest environment's tools (Nix in Nix).
    bin_dir = tmp_path / "bin"
    for name, command in (("cc", TEST_CC), ("c++", TEST_CXX), ("python3", TEST_PYTHON)):
        _write_executable(bin_dir / name, f'exec {command} "$@"\n')
    for name in ("CC", "CXX", "PKG_CONFIG", "PYTHON"):
        environment.pop(name, None)
    environment["IN_NIX_SHELL"] = "pure"
    expected_path = environment["PATH"]

    result = _run(root, environment, script_name)

    assert result.returncode == 42, result.stdout + result.stderr
    profile = "qemu-generic" if script_name == "build-qemu-generic.sh" else "qemu"
    captured = (root / "build" / profile / "configure-environment").read_text()
    assert captured.splitlines() == [
        "CC=cc", "CXX=c++", "PKG_CONFIG=pkg-config", "PYTHON=python3", "PATH=" + expected_path,
    ]


def test_old_build_reconfigures_once_and_honors_toolchain_changes(tmp_path: Path) -> None:
    # This fixture provides only configure/Meson control-plane effects. The
    # zlib admission above still uses the real active compiler/development lib.
    root, environment = _project(
        tmp_path, _real_zlib_flags(),
        configure_body=(
            'printf "%s\\n" "$CC" >> configure-calls\n'
            "printf \"%s\\n\" \"arm-softmmu = 'dm-mc02'\" > config-meson.cross\n"
            'touch build.ninja\n'
            'mkdir -p pyvenv/bin meson-private\n'
            'printf "buildtype = release\\nqom_cast_debug = false\\nplugins = false\\nwerror = false\\n" '
            '> meson-private/cmd_line.txt\n'
        ),
    )
    build = root / "build/qemu"
    build.mkdir(parents=True)
    (build / "build.ninja").write_text("# old project .venv Meson\n")
    (build / "guest-image.bin").write_bytes(b"preserve configured inputs")
    shutil.copy2(BUILD_SCRIPT.parent / "meson", root / "tools/meson")
    _write_executable(
        build / "pyvenv/bin/meson",
        'case "$1" in\n'
        '  --version) printf "1.2.3\\n" ;;\n'
        '  compile) cd "$3"; test -f .dm-toolchain; printf "%s\\n" compile >> compile-calls ;;\n'
        '  *) exit 91 ;;\nesac\n',
    )

    first = _run(root, environment)
    second = _run(root, environment)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert (build / "configure-calls").read_text().splitlines() == [TEST_CC]
    assert (build / "guest-image.bin").read_bytes() == b"preserve configured inputs"

    # An explicit valid compiler wrapper changes configure identity. It is
    # preserved, and must re-select the compiler even though a build exists.
    wrapper = tmp_path / "custom-cc"
    _write_executable(wrapper, f'exec {TEST_CC} "$@"\n')
    environment["CC"] = str(wrapper)
    changed = _run(root, environment)
    environment["QEMU_RECONFIGURE"] = "1"
    forced = _run(root, environment)
    assert changed.returncode == forced.returncode == 0, changed.stderr + forced.stderr
    assert (build / "configure-calls").read_text().splitlines() == [
        TEST_CC, str(wrapper), str(wrapper),
    ]
    assert (build / "compile-calls").read_text().splitlines() == ["compile"] * 4
    assert f"CC={wrapper}\n" in (build / ".dm-toolchain").read_text()

    # Nix dependency flags can change with a new flake lock even when a caller
    # keeps a compiler wrapper path. Cached Meson metadata must not survive it.
    environment.pop("QEMU_RECONFIGURE")
    environment["NIX_LDFLAGS"] = "-L/fixture/nix-dependency"
    changed_dependency = _run(root, environment)
    assert changed_dependency.returncode == 0, changed_dependency.stderr
    assert len((build / "configure-calls").read_text().splitlines()) == 4
