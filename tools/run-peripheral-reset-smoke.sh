#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || exit 1
[[ -x "$qemu_bin" ]] || exit 1
run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-peripheral-reset.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/reset.elf"
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
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_peripheral_reset_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_peripheral_reset_smoke.c"
"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
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

def words():
    text = command("human-monitor-command", {"command-line": "xp /5wx 0x20000000"}) or ""
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]

deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    value = words()
    if value and value[0] == 0x52535431:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("first reset phase was not observed: %r" % value)

command("system_reset")
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    value = words()
    if len(value) >= 5 and value[0] == 0x52535432:
        if value[1] != 0 or value[2] != 0:
            raise RuntimeError("CORDIC state survived system_reset: %r" % value[1:3])
        if value[3] != 1024:
            raise RuntimeError("USB GRXFSIZ did not reset: %d" % value[3])
        if value[4] != 2:
            raise RuntimeError("unexpected reset count: %d" % value[4])
        print("RESULT: peripheral warm-reset state smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("second reset phase was not observed: %r" % value)

command("quit")
sock.close()
PY
