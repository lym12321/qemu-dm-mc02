#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
source_dir="$root_dir/qemu/upstream"
build_dir="$root_dir/build/qemu-generic"

[[ -x "$source_dir/configure" ]] || {
    printf '%s\n' 'blocked: run git submodule update --init qemu/upstream first' >&2
    exit 1
}
command -v ninja >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: ninja is required (apt: ninja-build)' >&2
    exit 1
}

# QEMU's configure creates a per-build pyvenv for its pinned Meson.  Keep
# configure's bootstrap lookup on the system Python.
configure_path=/usr/bin:/bin:/usr/local/bin
configure_args=(
    --target-list=arm-softmmu
    --without-default-devices
    --disable-docs
    --disable-werror
    --disable-capstone
    --disable-slirp
    --enable-fdt=internal
    --enable-download
    --prefix="$root_dir/build/install-generic"
)

mkdir -p "$build_dir"
if [[ ! -f "$build_dir/build.ninja" ]]; then
    (
        cd "$build_dir"
        PATH="$configure_path" "$source_dir/configure" "${configure_args[@]}"
    )
fi

ninja -C "$build_dir" qemu-system-arm
printf 'Generic ARM QEMU build ready: %s\n' "$build_dir/qemu-system-arm"
