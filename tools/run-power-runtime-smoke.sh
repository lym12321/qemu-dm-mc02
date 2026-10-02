#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
guest_elf="$root_dir/build/smoke/dm_mc02_power_runtime_smoke.elf"

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU binary not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

mkdir -p -- "${guest_elf%/*}"
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_power_runtime_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_power_runtime_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02,vin-mv=0 -kernel "$guest_elf" \
    -nodefaults -display none -monitor none -serial none \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

path = sys.argv[1]
sock = QmpSession(path, timeout=2.0)

command = sock.command

def words():
    text = command("human-monitor-command", {
        "command-line": "xp /2wx 0x20000000",
    }) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

def wait_for(predicate, description):
    deadline = time.monotonic() + 2.0
    last = []
    while time.monotonic() < deadline:
        last = words()
        if predicate(last):
            return last
        time.sleep(0.01)
    raise RuntimeError("timeout waiting for %s: %r" % (description, last))

initial = words()
if initial != [0, 0]:
    raise RuntimeError("guest executed while initially unpowered: %r" % initial)
if command("qom-get", {"path": "/machine", "property": "mcu-power-good"}) is not False:
    raise RuntimeError("initial MCU power property was not low")

command("qom-set", {"path": "/machine", "property": "vin-mv", "value": "24000"})
if command("qom-get", {"path": "/machine", "property": "mcu-power-good"}) is not True:
    raise RuntimeError("MCU power property did not recover from startup off")
started = wait_for(lambda value: len(value) >= 2 and value[0] > 100 and value[1] > 0,
                   "guest counter to start")
before = started[0]
time.sleep(0.05)
running = words()
if running[0] <= before:
    raise RuntimeError("guest did not advance before power cut: %r" % running)

command("qom-set", {"path": "/machine", "property": "vin-mv", "value": "0"})
if command("qom-get", {"path": "/machine", "property": "mcu-power-good"}) is not False:
    raise RuntimeError("MCU power property did not go low")
frozen = words()
time.sleep(0.05)
frozen_again = words()
if frozen_again[0] != frozen[0] or frozen_again[1] != frozen[1]:
    raise RuntimeError("guest progressed while VIN=0: %r -> %r" %
                       (frozen, frozen_again))

command("qom-set", {"path": "/machine", "property": "vin-mv", "value": "24000"})
if command("qom-get", {"path": "/machine", "property": "mcu-power-good"}) is not True:
    raise RuntimeError("MCU power property did not recover")
restarted = wait_for(lambda value: len(value) >= 2 and value[1] >= 2 and
                     value[0] > frozen_again[0], "guest reset after power restore")

status = command("query-status")
if not isinstance(status, dict) or status.get("status") != "running":
    raise RuntimeError("QEMU did not remain running: %r" % status)
command("quit")
print("RESULT: VIN=0 MCU power-off and VIN restore/reset smoke passed")
print("  initial=%r started=%r frozen=%r restarted=%r" %
      (initial, started, frozen_again, restarted))
PY
