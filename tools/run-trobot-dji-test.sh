#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
[[ $# -ge 1 && $# -le 3 && -f "$1" && -x "$qemu_bin" ]] || {
    printf '%s\n' 'Usage: run-trobot-dji-test.sh DjiTest.elf [report.json [firmware-source]]' >&2
    exit 1
}
dji_python=${PYTHON:-"$root_dir/.venv/bin/python"}
[[ -x "$dji_python" ]] || dji_python=python3
exec "$dji_python" "$script_dir/dm_mc02_dji_test.py" "$1" \
    "${2:-"$root_dir/build/test-results/trobot-dji.json"}" "${3:-"$root_dir/../trobot"}" \
    --qemu "$qemu_bin"
