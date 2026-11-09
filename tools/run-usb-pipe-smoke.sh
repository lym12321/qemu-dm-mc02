#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
exec "${PYTHON:-python3}" "$root_dir/smoke/trobot_usb_smoke.py" \
    --qemu "${QEMU_SYSTEM_ARM:-$root_dir/build/qemu/qemu-system-arm}" "$@"
