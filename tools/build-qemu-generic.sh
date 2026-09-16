#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
source_dir="$root_dir/qemu/upstream"
build_dir="$root_dir/build/qemu-generic"

[[ -x "$source_dir/configure" ]] || {
    printf '%s\n' 'blocked: QEMU source is missing; run tools/bootstrap-qemu.sh first' >&2
    exit 1
}
[[ -x "$root_dir/.venv/bin/ninja" ]] || {
    printf '%s\n' 'blocked: uv environment is missing; run uv sync --group dev' >&2
    exit 1
}

# QEMU's configure creates a per-build pyvenv for Meson and requires a Python
# installation with ensurepip.  The project uv environment is used for the
# build command below; keep configure's bootstrap lookup on the system Python
# because this workspace's uv environment intentionally has no ensurepip.
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

export PATH="$root_dir/.venv/bin:$PATH"
"$root_dir/.venv/bin/ninja" -C "$build_dir" qemu-system-arm
printf 'Generic ARM QEMU build ready: %s\n' "$build_dir/qemu-system-arm"
