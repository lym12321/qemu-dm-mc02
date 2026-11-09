#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir:$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02,board-profile=STM32H723-EVAL \
    -nodefaults -display none -monitor none -serial none -S \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import sys

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

profile = command("qom-get", {"path": "/machine", "property": "board-profile"})
assert profile == "STM32H723-EVAL", profile
power = command("qom-get", {"path": "/machine", "property": "mcu-power-good"})
assert power is True, power

before = command("qom-get", {"path": "/machine", "property": "board-state"})
assert before["board"] == "STM32H723-EVAL", before
assert before["key"] == {"pin": "A13", "pressed": False, "level": 1}, before
assert before["led"]["pin"] == "A0", before
command("qom-set", {"path": "/machine", "property": "gpio-input", "value": "B0=1"})
command("qom-set", {"path": "/machine", "property": "user-key", "value": True})
command("qom-set", {"path": "/machine", "property": "vin-mv", "value": "25000"})
after = command("qom-get", {"path": "/machine", "property": "board-state"})
assert after["timestamp_ns"] == before["timestamp_ns"], (before, after)
assert after["key"]["level"] == 0 and after["key"]["pressed"] is True
assert after["gpio"]["B"]["sampled"] & 1 == 1
assert after["vin_mv"] == 25000
command("system_reset")
after_reset = command("qom-get", {"path": "/machine", "property": "board-state"})
assert after_reset["key"]["level"] == 0
assert after_reset["gpio"]["B"]["sampled"] & 1 == 1
command("qom-set", {"path": "/machine", "property": "user-key", "value": False})
assert command("qom-get", {"path": "/machine", "property": "board-state"})["key"]["level"] == 1

print("RESULT: alternate STM32H723 board profile QEMU smoke passed")
print("  profile=STM32H723-EVAL, UARTs=2, FDCAN=1, mcu-power-good=true")
PY
