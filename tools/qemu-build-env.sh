#!/usr/bin/env bash
# Sourced by both QEMU profiles: each build uses one dependency environment.
if [[ -n ${IN_NIX_SHELL:-} ]]; then
    export CC=${CC:-cc}
    export CXX=${CXX:-c++}
    export PKG_CONFIG=${PKG_CONFIG:-pkg-config}
    export PYTHON=${PYTHON:-python3}
else
    export PATH=/usr/bin:/bin
    export CC=${CC:-/usr/bin/cc}
    export CXX=${CXX:-/usr/bin/c++}
    export PKG_CONFIG=${PKG_CONFIG:-/usr/bin/pkg-config}
    export PYTHON=${PYTHON:-/usr/bin/python3}
fi

qemu_normalize_meson_defaults() {
    # QEMU 8.2 configure writes werror=true for Git sources even with
    # --disable-werror. Meson 1.2 can reapply that native-file default on
    # regeneration, so keep it consistent with both project build profiles.
    local native_file=$1
    if [[ -f "$native_file" ]]; then
        sed -i 's/^werror = true$/werror = false/' "$native_file"
    fi
}

qemu_toolchain_identity() {
    local name
    for name in PATH CC CXX PKG_CONFIG PYTHON CFLAGS CXXFLAGS OBJCFLAGS LDFLAGS \
                AR AS NM RANLIB LD OBJCOPY STRIP DLLTOOL WIDL WINDRES WINDMC \
                PKG_CONFIG_PATH PKG_CONFIG_LIBDIR LD_LIBRARY_PATH SDL2_CONFIG \
                NIX_CC NIX_CFLAGS_COMPILE NIX_LDFLAGS; do
        printf '%s=%s\n' "$name" "${!name-}"
    done
    printf 'python_path=%s\n' "$(command -v "$PYTHON")"
    if (($#)); then
        printf '%s\n' "$@"
    fi
}

qemu_check_prerequisites() {
    local -a missing_prerequisites=()
    local requirement command_name package_name qemu_python
    for requirement in "${CC%% *}:build-essential" "${CXX%% *}:build-essential" "$PKG_CONFIG:pkg-config" "ninja:ninja-build"; do
        command_name=${requirement%%:*}
        package_name=${requirement#*:}
        if ! command -v "$command_name" >/dev/null 2>&1; then
            missing_prerequisites+=("$command_name (apt: $package_name)")
        fi
    done

    if command -v "$PKG_CONFIG" >/dev/null 2>&1; then
        if ! "$PKG_CONFIG" --atleast-version=2.56 glib-2.0; then
            missing_prerequisites+=("glib-2.0 >= 2.56 (apt: libglib2.0-dev)")
        fi
        if ! "$PKG_CONFIG" --exists zlib; then
            missing_prerequisites+=("zlib (apt: zlib1g-dev)")
        fi
    fi

    qemu_python=${PYTHON:-python3}
    if ! command -v "$qemu_python" >/dev/null 2>&1; then
        missing_prerequisites+=("Python 3.11+ (apt: python3)")
    elif ! "$qemu_python" -c '
import importlib.util
import sys

pip = importlib.util.find_spec("pip")
setuptools = importlib.util.find_spec("setuptools")
ensurepip = importlib.util.find_spec("ensurepip")
valid = (
    sys.version_info >= (3, 11)
    and importlib.util.find_spec("venv")
    and ((pip and setuptools) or ensurepip)
)
sys.exit(not bool(valid))
    ' >/dev/null 2>&1; then
        missing_prerequisites+=("Python 3.11+ with venv bootstrap (apt: python3-venv)")
    fi

    if ((${#missing_prerequisites[@]})); then
        printf '%s\n' 'blocked: missing QEMU build prerequisites:' >&2
        printf '  - %s\n' "${missing_prerequisites[@]}" >&2
        printf '%s\n' 'Install the listed packages, then rerun this script.' >&2
        printf '%s\n' 'Alternatively, enter the complete environment with nix develop.' >&2
        return 1
    fi

    # pkg-config metadata can survive missing headers/libraries or point outside
    # the selected compiler's sysroot. Check the actual C boundary before configure.
    if ! "$qemu_python" - <<'PY'
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

try:
    zlib_flags = subprocess.check_output(
        [os.environ["PKG_CONFIG"], "--cflags", "--libs", "zlib"], text=True
    )
    with tempfile.TemporaryDirectory(prefix="dm-qemu.zlib.", dir="/tmp") as probe_dir:
        command = [
            *shlex.split(os.environ["CC"]),
            *shlex.split(os.environ.get("CFLAGS", "")),
            "-x", "c", "-", "-x", "none", "-o", str(Path(probe_dir) / "probe"),
            *shlex.split(os.environ.get("LDFLAGS", "")),
            *shlex.split(zlib_flags),
        ]
        subprocess.run(
            command,
            input="#include <zlib.h>\nint main(void) { return !zlibVersion(); }\n",
            text=True, check=True,
        )
except (OSError, ValueError, subprocess.CalledProcessError) as error:
    print(f"zlib compiler/link check failed: {error}", file=sys.stderr)
    sys.exit(1)
PY
    then
        printf '%s\n' \
            'blocked: zlib headers or library are unusable (apt: zlib1g-dev).' \
            'Check that CC and pkg-config use the same development libraries/sysroot.' \
            'Use nix develop for Nix dependencies, or zlib1g-dev for the distro toolchain.' >&2
        return 1
    fi
}
