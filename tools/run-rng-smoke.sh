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

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-rng.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/rng.elf"
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
    -Wl,-T,"$root_dir/smoke/dm_mc02_rng_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_rng_smoke.c"

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
        "command-line": "xp /24wx 0x20000000"}) or ""
    return [int(value, 16) for value in
            re.findall(r"0x([0-9a-fA-F]{8})", text)]

command("cont")
deadline = time.monotonic() + 3.0
last = []
while time.monotonic() < deadline:
    last = words()
    # RESULT[0] is an early-start marker.  RESULT[19] is written only after
    # all guest checks and the second IRQ observation have completed.
    if len(last) >= 24 and last[19] == 0x444f4e45:
        break
    time.sleep(0.005)
else:
    raise RuntimeError("RNG result was not observed: %r" % last)

if last[1:4] != [0, 0, 0]:
    raise RuntimeError("disabled reset state mismatch: %r" % last[1:4])
if not (last[4] & 4) or not (last[5] & 1):
    raise RuntimeError("enable/DRDY state mismatch: %r" % last[4:6])
expected = [0x40aec71f, 0x91e00c19, 0x9c0fe128,
            0x6570f69d, 0x0fce02cc, 0x3d6b45e7]
if (last[6:10] != expected[:4] or last[15] != expected[4] or
        last[17] != expected[5]):
    raise RuntimeError("RNG data sequence mismatch: words=%r refill=%#x next=%#x expected=%r" %
                       (last[6:10], last[15], last[17], expected))
if last[10] & 1:
    raise RuntimeError("DRDY remained set after four reads: %#x" % last[10])
if last[11] != 0x49524e47:
    raise RuntimeError("RNG IRQ 80 was not reached: %#x" % last[11])
if not (last[12] & 1) or (last[13] & 8):
    raise RuntimeError("IRQ status/disable mismatch: %#x %#x" %
                       (last[12], last[13]))
if not (last[14] & 1) or not last[15] or not (last[16] & 1):
    raise RuntimeError("RNG refill boundary mismatch: %r" % last[14:17])
if last[17] == 0 or not (last[18] & 1) or last[19] != 0x444f4e45:
    raise RuntimeError("RNG refill/completion mismatch: %r" % last[17:20])
if last[20] != 0x49524e47 or not (last[21] & 1) or (last[22] & 8):
    raise RuntimeError("RNG rearmed IRQ mismatch: %r" % last[20:23])
if last[23] != 2:
    raise RuntimeError("RNG IRQ count mismatch: %#x" % last[23])

print("RESULT: STM32H723 RNG register, four-word buffer and IRQ smoke passed")
command("quit")
sock.close()
PY
