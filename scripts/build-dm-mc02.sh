#!/usr/bin/env bash
set -Eeuo pipefail

source_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build_dir=${1:-"$source_dir/build"}
mkdir -p -- "$build_dir"
build_dir=$(CDPATH= cd -- "$build_dir" && pwd)
build_type=${QEMU_BUILD_TYPE:-release}
jobs=${QEMU_BUILD_JOBS:-4}

case "$build_type" in
    debug|debugoptimized|release|minsize|plain) ;;
    *) printf 'Invalid QEMU_BUILD_TYPE: %s\n' "$build_type" >&2; exit 2 ;;
esac

if [[ ! -f "$build_dir/build.ninja" || ${QEMU_RECONFIGURE:-0} == 1 ]]; then
    qemu_python=${PYTHON:-python3}
    prefix="$build_dir/install"
    case "$(uname -s)" in
        MINGW*|MSYS*)
            # MSYS2's system Python is externally managed. The bundled Meson
            # wheel is installed only in this build's bootstrap environment.
            "$qemu_python" -m venv --system-site-packages "$build_dir/bootstrap-python"
            qemu_python="$build_dir/bootstrap-python/bin/python.exe"
            "$qemu_python" -m pip install --no-index \
                --find-links "$source_dir/python/wheels" meson==1.2.3
            prefix=$(cygpath -m "$prefix")
            ;;
    esac
    (
        cd -- "$build_dir"
        "$source_dir/configure" --python="$qemu_python" \
            --target-list=arm-softmmu --with-devices-arm=dm-mc02 \
            --prefix="$prefix" --disable-docs --disable-werror \
            --disable-capstone --disable-slirp --disable-sdl --disable-gtk \
            --disable-plugins --enable-fdt=internal --enable-download \
            -Dbuildtype="$build_type" -Dqom_cast_debug=false
    )
fi

exe_suffix=
case "$(uname -s)" in MINGW*|MSYS*) exe_suffix=.exe ;; esac
ninja -C "$build_dir" -j "$jobs" "qemu-system-arm$exe_suffix"
