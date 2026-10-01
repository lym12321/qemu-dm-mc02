#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-tim8-dbm.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/tim8-dbm.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_tim8_dbm_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_tim8_dbm_smoke.c"

"$qemu_bin" -machine dm-mc02,accurate-timing=on -kernel "$guest_elf" \
    -nodefaults -display none -monitor none -serial none -d guest_errors \
    -D /tmp/dm-mc02-tim8-dbm.log \
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

def word(address):
    text = command("human-monitor-command", {"command-line": f"xp /1wx {address:#x}"}) or ""
    values = re.findall(r"0x([0-9a-fA-F]{8})", text)
    return int(values[0], 16) if values else None

seen = []
switched_to_m1 = False
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    marker = word(0x20000000)
    ccr = word(0x40010434)
    if marker != 0x44424D31:
        time.sleep(0.001)
        continue
    if ccr in (101, 102, 103, 104, 201, 202, 203, 204):
        if not seen or seen[-1] != ccr:
            seen.append(ccr)
            if len(seen) >= 2 and seen[-2:] == [104, 201]:
                switched_to_m1 = True
    if switched_to_m1 and ccr == 204:
        cr = word(0x400204a0)
        ndtr = word(0x400204a4)
        m0ar = word(0x400204ac)
        m1ar = word(0x400204b0)
        if cr is None or ndtr is None or m0ar is None or m1ar is None:
            raise RuntimeError("DBM registers unavailable")
        if not (cr & (1 << 18)) or (cr & (1 << 8)) or (cr & (1 << 19)):
            raise RuntimeError(f"unexpected DBM/CT after two buffers: {cr:#x}")
        if ndtr != 4:
            raise RuntimeError(f"NDTR was not reloaded: {ndtr}")
        if m0ar != 0x20000100 or m1ar != 0x20000108:
            raise RuntimeError(f"buffer pointers not alternated: {m0ar:#x} {m1ar:#x}")
        print("RESULT: TIM8 DMA double-buffer smoke passed")
        break
    time.sleep(0.001)
else:
    raise RuntimeError(f"DBM transfer sequence was not observed: {seen}")
command("quit")
sock.close()
PY
