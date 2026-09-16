#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
source_dir="$root_dir/qemu/upstream"

if [[ ! -f "$source_dir/configure" ]]; then
    printf '%s\n' 'blocked: QEMU source is missing; bootstrap it with a fixed ref first' >&2
    exit 1
fi
if [[ ! -f "$source_dir/hw/arm/dm_mc02.c" ]]; then
    printf '%s\n' 'blocked: expected M1 source is not present; this checkout needs the project patch applied' >&2
    exit 1
fi
if ! rg -q "dm_mc02.c" "$source_dir/hw/arm/meson.build"; then
    printf '%s\n' 'blocked: dm_mc02.c is not registered in hw/arm/meson.build' >&2
    exit 1
fi
printf '%s\n' 'DM-MC02 QEMU patch check: applied (source and meson registration present)'
