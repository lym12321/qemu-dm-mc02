#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-dma-dbm-reconfigure.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/dma-dbm-reconfigure.elf"
qemu_pid=''
cleanup() {
    local status=$?
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
    return "$status"
}
trap cleanup EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_dma_dbm_reconfigure_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_dma_dbm_reconfigure_smoke.c"

"$qemu_bin" -machine dm-mc02,accurate-timing=on -kernel "$guest_elf" \
    -nodefaults -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def words(address, count):
    text = command("human-monitor-command", {
        "command-line": f"xp /{count}wx {address:#x}"
    }) or ""
    return [int(value, 16) for value in
            re.findall(r"0x([0-9a-fA-F]{8})", text)]

deadline = time.monotonic() + 5.0
while time.monotonic() < deadline:
    result = words(0x20000000, 11)
    if len(result) >= 11 and result[0] == 0x52434631 and \
            result[10] == 0x52434632:
        if not (result[1] & (1 << 18)) or not (result[1] & (1 << 19)):
            raise RuntimeError(f"DBM did not switch to M1: {result!r}")
        if result[3] & (1 << 19):
            raise RuntimeError(f"DBM did not switch back to M0: {result!r}")
        if result[4] != 3:
            raise RuntimeError(f"new M0 did not start with NDTR=3: {result!r}")
        if result[5] != 301:
            raise RuntimeError(f"replacement buffer was not delivered: {result!r}")
        if result[2] != result[7]:
            raise RuntimeError(f"M0AR base was not retained: {result!r}")
        if not (result[6] & (1 << 18)):
            raise RuntimeError(f"DBM was lost after reconfiguration: {result!r}")
        if result[8] == result[2]:
            raise RuntimeError(f"M1AR unexpectedly changed: {result!r}")
        if result[9] & 0x300000 != 0x300000:
            raise RuntimeError(f"HT/TC status was not retained: {result!r}")
        print("RESULT: DMA DBM inactive-buffer reconfiguration smoke passed")
        break
    time.sleep(0.005)
else:
    raise RuntimeError(f"DBM reconfiguration result was not observed: {result!r}")

command("quit")
sock.close()
PY
