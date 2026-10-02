#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || exit 1
[[ -x "$qemu_bin" ]] || exit 1

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-exti.XXXXXX")
guest_elf="$run_dir/exti.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_exti_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_exti_smoke.c"

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
        "command-line": "xp /6wx 0x20000000"}) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

def gpioa_idr():
    text = command("human-monitor-command", {
        "command-line": "xp /1wx 0x58020010"}) or ""
    values = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]
    return values[0] if values else None

def reg(address):
    text = command("human-monitor-command", {
        "command-line": "xp /1wx 0x%08x" % address}) or ""
    values = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]
    return values[0] if values else None

command("cont")
last = None
for _ in range(3000):
    last = words()
    if len(last) >= 2 and last[1] == 1:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("EXTI IRQ result was not observed: %r" % (last,))

if last[0] != 0x45585431 or last[1] != 1:
    raise RuntimeError("EXTI marker/count mismatch: %r" % (last,))
if last[2] != 0x45585432 or not (last[3] & (1 << 15)):
    raise RuntimeError("EXTI pending/IRQ mismatch: %r" % (last,))
if last[4] & (1 << 15):
    raise RuntimeError("EXTI PR was not cleared: %r" % (last,))
command("qom-set", {"path": "/machine", "property": "user-key",
                     "value": True})
for _ in range(3000):
    last = words()
    if len(last) >= 2 and last[1] == 2:
        break
    time.sleep(0.001)
else:
    debug = command("human-monitor-command", {
        "command-line": "xp /2wx 0x58000014"}) or ""
    idr = gpioa_idr()
    raise RuntimeError("EXTI GPIO input edge was not observed: %r pr=%s idr=%r" %
                       (last, debug, idr))
if last[1] != 2 or last[3] != (1 << 15) or gpioa_idr() & (1 << 15):
    raise RuntimeError("EXTI GPIO input/IRQ mismatch: %r idr=%r" %
                       (last, gpioa_idr()))
command("qom-set", {"path": "/machine", "property": "gpio-input",
                     "value": "B14=1"})
command("qom-set", {"path": "/machine", "property": "gpio-input",
                     "value": "A14=1"})
command("qom-set", {"path": "/machine", "property": "gpio-input",
                     "value": "A14=0"})
# The guest remaps EXTI14 from GPIOA to GPIOB in the count-3 handler. GPIOB
# was already high, so the remap itself must use the new source.
for _ in range(3000):
    last = words()
    if len(last) >= 6 and last[5] == 0x444f4e45 and last[1] == 4:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("EXTI remapped GPIO input edge was not observed: %r "
                       "exticr=%r gpiob=%r rtsr=%r line_pr=%r" %
                       (last, reg(0x58000414), reg(0x58020410),
                        reg(0x58000000), reg(0x58000014)))
if last[3] != (1 << 14) or not (reg(0x58020410) & (1 << 14)):
    raise RuntimeError("EXTI remapped GPIO input mismatch: %r gpiob=%r" %
                       (last, reg(0x58020410)))
print("RESULT: EXTI software/GPIO/generic-input pending/IRQ/W1C smoke passed")
command("quit")
sock.close()
PY
