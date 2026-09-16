#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_build_dir=${QEMU_BUILD_DIR:-"$root_dir/build/qemu"}
power_object="$qemu_build_dir/libqemu-arm-softmmu.fa.p/hw_arm_dm_mc02_power.c.o"
out_dir="$root_dir/build/smoke"
binary="$out_dir/dm_mc02_power_boundary_smoke"

command -v cc >/dev/null 2>&1 || { printf '%s\n' 'blocked: cc is required' >&2; exit 1; }
command -v pkg-config >/dev/null 2>&1 || { printf '%s\n' 'blocked: pkg-config is required' >&2; exit 1; }
pkg-config --exists glib-2.0 || { printf '%s\n' 'blocked: glib-2.0 development files are required' >&2; exit 1; }
[[ -f "$power_object" ]] || {
    printf 'blocked: QEMU power object not found: %s\n' "$power_object" >&2
    printf '%s\n' 'build QEMU first with tools/build-qemu.sh, or set QEMU_BUILD_DIR' >&2
    exit 1
}
mkdir -p "$out_dir"

cc -std=gnu11 -O2 -g -Wall -Wextra -Wpedantic \
    $(pkg-config --cflags glib-2.0) \
    -I"$qemu_build_dir" -I"$root_dir/qemu/upstream" \
    -I"$root_dir/qemu/upstream/include" \
    -I"$qemu_build_dir/target/arm" \
    -I"$root_dir/qemu/upstream/target/arm" \
    -I"$root_dir/qemu/upstream/linux-headers" \
    -I"$qemu_build_dir/linux-headers" \
    -DNEED_CPU_H -D_GNU_SOURCE -D_FILE_OFFSET_BITS=64 \
    -DCONFIG_TARGET='"arm-softmmu-config-target.h"' \
    -DCONFIG_DEVICES='"arm-softmmu-config-devices.h"' \
    -o "$binary" "$root_dir/smoke/dm_mc02_power_boundary_smoke.c" \
    "$power_object"

"$binary"
