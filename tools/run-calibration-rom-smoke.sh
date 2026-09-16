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

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-calibration-rom.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/calibration-rom.elf"
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
    -Wl,-T,"$root_dir/smoke/dm_mc02_calibration_rom_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_calibration_rom_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -qmp \
    "unix:$qmp_socket,server=on,wait=off" \
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
        "command-line": "xp /14wx 0x20000000"
    }) or ""
    return [int(value, 16) for value in
            re.findall(r"0x([0-9a-fA-F]{8})", text)]

deadline = time.monotonic() + 2.0
result = []
while time.monotonic() < deadline:
    result = words()
    if len(result) >= 14 and result[0] == 0x43414C31 and result[13] == 0x43414C32:
        break
    time.sleep(0.005)
else:
    raise RuntimeError(f"calibration ROM result was not observed: {result!r}")

expected = [0x12345678, 0x9ABCDEF0, 0x13579BDF, 1000, 1500, 1500]
before = result[1:7]
after = result[7:13]
if before != expected:
    raise RuntimeError(
        f"calibration ROM initial contents mismatch: expected {expected!r}, got {before!r}")
if after != before:
    changed = [(hex(0x1FF1E000 + offset), old, new)
               for offset, old, new in zip(
                   (0x800, 0x804, 0x808, 0x820, 0x840, 0x860), before, after)
               if old != new]
    raise RuntimeError(
        "calibration ROM write was accepted: "
        f"offset/old/new={changed!r}")

print("RESULT: calibration ROM UID/calibration reads and ignored writes passed")
command("quit")
sock.close()
PY
