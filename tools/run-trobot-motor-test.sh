#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
[[ $# -ge 1 && $# -le 2 ]] || {
    printf 'usage: %s MotorTest.elf [report.json]\n' "$0" >&2
    exit 2
}
exec "${PYTHON:-python3}" "$script_dir/dm_mc02_motor_test.py" "$1" \
    "${2:-$root_dir/build/test-results/trobot-motor.json}" \
    --qemu "${QEMU_SYSTEM_ARM:-$root_dir/build/qemu/qemu-system-arm}"
