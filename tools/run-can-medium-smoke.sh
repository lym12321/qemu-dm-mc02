#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-can-medium.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/can-medium.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_can_medium_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_can_medium_smoke.c"

"$qemu_bin" -object can-bus,id=canbus \
    -machine dm-mc02,canbus=canbus -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -d guest_errors \
    -D /tmp/dm-mc02-can-medium.log \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def words(command_line):
    text = command("human-monitor-command", {"command-line": command_line}) or ""
    return [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text)]

deadline = time.monotonic() + 3.0
last = []
while time.monotonic() < deadline:
    last = words("xp /18wx 0x20000000")
    if len(last) >= 18 and last[0] == 0x434d4431 and last[17] == 1:
        if last[1:3] != [0x321, 0xa5]:
            raise RuntimeError("FDCAN1->FDCAN2 mismatch: %r" % last[1:3])
        if last[4:6] != [0x220, 0x33]:
            raise RuntimeError("FDCAN2->FDCAN3 mismatch: %r" % last[4:6])
        if last[7:11] != [0x500, 0x11, 0x100, 0x22]:
            raise RuntimeError("standard bus ordering mismatch: %r" % last[7:11])
        if last[12:15] != [0, 0, 0]:
            raise RuntimeError("unexpected CAN error counters: %r" % last[12:15])
        if (last[15] & 3) != 3 or not (last[16] & 1):
            raise RuntimeError("TX completion missing: %r" % last[15:17])
        print("RESULT: FDCAN standard CAN bus smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("CAN medium result was not observed: %r" % last)
command("quit")
sock.close()
PY
