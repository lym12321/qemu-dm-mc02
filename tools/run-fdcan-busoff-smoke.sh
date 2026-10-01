#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-fdcan-busoff.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/fdcan-busoff.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_fdcan_busoff_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_fdcan_busoff_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S -serial none \
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

def words(address, count):
    text = command("human-monitor-command", {
        "command-line": f"xp /{count}wx {address}"
    }) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

command("cont")
deadline = time.monotonic() + 3.0
last = []
while time.monotonic() < deadline:
    last = words("0x20000000", 8)
    if len(last) >= 8 and last[7] == 0x424f3131:
        if (last[0] & 0xff) != 255:
            raise RuntimeError(f"unexpected TEC: {last!r}")
        if not (last[1] & (1 << 7)):
            raise RuntimeError(f"PSR.BO not set: {last!r}")
        if not (last[2] & (1 << 25)):
            raise RuntimeError(f"IR.BO not set: {last!r}")
        if not (last[3] & 1):
            raise RuntimeError(f"CCCR.INIT not set: {last!r}")
        if last[4] != 0 or (last[5] & (1 << 7)) or (last[6] & 1):
            raise RuntimeError(f"bus-off recovery mismatch: {last!r}")
        print("RESULT: FDCAN bus-off and explicit recovery smoke passed")
        break
    if len(last) >= 8 and last[7] == 0x424f4641:
        raise RuntimeError(f"guest bus-off assertions failed: {last!r}")
    time.sleep(0.01)
else:
    raise RuntimeError(f"FDCAN bus-off result was not observed: {last!r}")
command("quit")
sock.close()
PY
