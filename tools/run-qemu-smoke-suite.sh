#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

mapfile -d '' smoke_scripts < <(
    find "$script_dir" -maxdepth 1 -type f -name 'run-*-smoke.sh' \
        ! -name 'run-qemu-smoke-suite.sh' -print0 | sort -z
)
if ((${#smoke_scripts[@]} == 0)); then
    printf '%s\n' 'no QEMU smoke scripts found' >&2
    exit 1
fi

failed=0
passed=0
for smoke in "${smoke_scripts[@]}"; do
    name=${smoke##*/}
    printf '=== %s ===\n' "$name"
    if bash "$smoke"; then
        passed=$((passed + 1))
    else
        printf 'FAILED: %s\n' "$name" >&2
        failed=$((failed + 1))
    fi
done

printf 'QEMU smoke summary: %d passed, %d failed, %d total\n' \
       "$passed" "$failed" "${#smoke_scripts[@]}"
if ((failed != 0)); then
    exit 1
fi
printf '%s\n' 'RESULT: QEMU smoke suite passed'
