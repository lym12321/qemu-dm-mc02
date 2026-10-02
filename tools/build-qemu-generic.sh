#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
source "$script_dir/qemu-build-env.sh"
source_dir="$root_dir/qemu/upstream"
build_dir="$root_dir/build/qemu-generic"

[[ -x "$source_dir/configure" ]] || {
    printf '%s\n' 'blocked: run git submodule update --init qemu/upstream first' >&2
    exit 1
}
qemu_check_prerequisites

# QEMU's configure owns the build-local Meson and compiler metadata.
configure_args=(
    --target-list=arm-softmmu
    --without-default-devices
    --disable-docs
    --disable-werror
    --disable-capstone
    --disable-slirp
    --disable-sdl
    --disable-gtk
    --enable-fdt=internal
    --enable-download
    --prefix="$root_dir/build/install-generic"
)

mkdir -p "$build_dir"
toolchain_identity=$(qemu_toolchain_identity "${configure_args[@]}")
if [[ ! -f "$build_dir/build.ninja" || ! -f "$build_dir/.dm-toolchain" ||
      "$(<"$build_dir/.dm-toolchain")" != "$toolchain_identity" ||
      "${QEMU_RECONFIGURE:-0}" == 1 ]]; then
    (
        cd "$build_dir"
        "$source_dir/configure" "${configure_args[@]}"
    )
    printf '%s\n' "$toolchain_identity" > "$build_dir/.dm-toolchain"
fi

qemu_normalize_meson_defaults "$build_dir/config-meson.cross"
ninja -C "$build_dir" qemu-system-arm
printf 'Generic ARM QEMU build ready: %s\n' "$build_dir/qemu-system-arm"
