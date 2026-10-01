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

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-tim3-irq.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/tim3-irq.elf"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_tim3_irq_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_tim3_irq_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    printf '%s\n' 'RESULT: TIM3 IRQ smoke failed (QMP socket missing)' >&2
    exit 1
}

python3 - "$qmp_socket" <<'PY'
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

command("cont")
last = None
for _ in range(3000):
    last = words()
    if len(last) >= 6 and last[5] == 0x444f4e45:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("TIM3 IRQ result was not observed: %r" % (last,))

if last[0] != 0x54494d33 or last[1] != 1:
    raise RuntimeError("TIM3 marker/count mismatch: %r" % (last,))
if last[2] != 0x49525133 or not (last[3] & 1):
    raise RuntimeError("TIM3 IRQ/UIF mismatch: %r" % (last,))
if last[4] & 1:
    raise RuntimeError("TIM3 UIF was not cleared: %r" % (last,))
print("RESULT: TIM3 update UIF/IRQ29/W0C smoke passed")
command("quit")
sock.close()
PY
