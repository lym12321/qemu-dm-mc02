#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
report_only=0

if [[ ${1:-} == --report-only ]]; then
    report_only=1
    shift
fi
if (($# != 0)); then
    printf 'usage: %s [--report-only]\n' "$0" >&2
    exit 2
fi

failures=0
lock_file="$root_dir/qemu.lock"
printf 'DM-MC02 QEMU environment check\nroot: %s\n' "$root_dir"

if command -v qemu-system-arm >/dev/null 2>&1; then
    qemu_bin=$(command -v qemu-system-arm)
    qemu_version=$(qemu-system-arm --version 2>/dev/null | sed -n '1p') || qemu_version='version query failed'
    printf 'qemu-system-arm: FOUND (%s)\n' "$qemu_bin"
    printf 'qemu-version: %s\n' "$qemu_version"
    printf '%s\n' 'ARM machines:'
    qemu-system-arm -machine help 2>&1 | sed -n '1,12p'
    printf '%s\n' 'M-profile-like CPU entries:'
    cpu_help=$(qemu-system-arm -cpu help 2>&1 || true)
    printf '%s\n' "$cpu_help" | rg -i 'cortex-m|m7|m4|m33|m55' || printf '%s\n' '  none reported'
    if printf '%s\n' "$cpu_help" | rg -qi 'cortex-m7|cortex-m[0-9]'; then
        printf '%s\n' 'M-profile CPU probe: FOUND'
    else
        printf '%s\n' 'M-profile CPU probe: BLOCKED (no Cortex-M CPU reported by this binary)'
        failures=$((failures + 1))
    fi
else
    printf '%s\n' 'qemu-system-arm: MISSING (install qemu-system-arm to run system smoke)'
    failures=$((failures + 1))
fi

source_dir="$root_dir/qemu/upstream"
if [[ -d "$source_dir/.git" ]]; then
    printf 'qemu-source: FOUND (%s)\n' "$source_dir"
    printf 'qemu-source-revision: %s\n' "$(git -C "$source_dir" rev-parse --short HEAD 2>/dev/null || printf unknown)"
else
    printf '%s\n' 'qemu-source: MISSING (run bootstrap-qemu.sh --download --ref <tag-or-commit>)'
    failures=$((failures + 1))
fi

if [[ -f "$lock_file" ]]; then
    printf '%s\n' 'qemu.lock:'
    sed -n '1,24p' "$lock_file"
else
    printf '%s\n' 'qemu.lock: MISSING'
    failures=$((failures + 1))
fi

custom_qemu="$root_dir/build/qemu/qemu-system-arm"
if [[ -x "$custom_qemu" ]] && "$custom_qemu" -machine help 2>/dev/null | rg -q '^dm-mc02[[:space:]]'; then
    printf 'dm-mc02 machine: FOUND (%s)\n' "$custom_qemu"
else
    printf '%s\n' 'dm-mc02 machine: pending (run tools/build-qemu.sh)'
fi

if ((report_only)); then
    exit 0
fi
if ((failures != 0)); then
    printf 'RESULT: BLOCKED (%d prerequisite(s) missing)\n' "$failures"
    exit 1
fi
printf '%s\n' 'RESULT: QEMU prerequisites found; inspect dm-mc02 machine status above'
