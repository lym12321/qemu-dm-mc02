#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'RESULT: blocked (arm-none-eabi-gcc is required)' >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'RESULT: blocked (python3 is required)' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || {
    printf 'RESULT: blocked (QEMU not found: %s)\n' "$qemu_bin" >&2
    exit 1
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-ovrmod.XXXXXX")
guest_elf="$run_dir/adc-ovrmod.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_adc_ovrmod_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_ovrmod_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def words():
    text = command("human-monitor-command", {
        "command-line": "xp /7wx 0x20001000"}) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

last = None
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    last = words()
    if len(last) >= 7 and last[6] == 0x444f4e45:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("ADC OVRMOD result was not observed: %r" % (last,))

if last[0] != 0x414f5631 or last[1] != 0x49525131:
    raise RuntimeError("marker or IRQ mismatch: %r" % (last,))
if not (last[2] & (1 << 2)) or not (last[2] & (1 << 4)):
    raise RuntimeError("OVRMOD interrupt did not observe EOC+OVR: %#x" % last[2])
if last[3] != 0x0200:
    raise RuntimeError("OVRMOD did not expose newest sample: %#x" % last[3])
if not (last[4] & (1 << 4)):
    raise RuntimeError("reading ADC_DR cleared OVR unexpectedly: %#x" % last[4])
if last[5] & (1 << 4):
    raise RuntimeError("OVR W1C clear failed: %#x" % last[5])
print("RESULT: ADC OVRMOD=1 overwrite smoke passed")
print("  EOC+OVR: %#x, newest ADC_DR: %#x" % (last[2], last[3]))
command("quit")
sock.close()
PY
