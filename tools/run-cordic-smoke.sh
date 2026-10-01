#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-cordic.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/cordic.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_cordic_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cordic_smoke.c"

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

deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /11wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 11 and words[0] == 0x434f5231:
        if words[1] != 0:
            raise RuntimeError("CORDIC reset CSR mismatch: %#x" % words[1])
        if not (words[2] & (1 << 31)) or words[2] & (1 << 30):
            raise RuntimeError("CORDIC result status mismatch: %#x" % words[2])
        if abs(words[3] - 0x5a82799a) > 2 or abs(words[4] - 0x5a82799a) > 2:
            raise RuntimeError("CORDIC pi/4 result mismatch: %r" % words[3:5])
        if words[5] & (1 << 31):
            raise RuntimeError("CORDIC RRDY not cleared: %#x" % words[5])
        if abs(words[6] - 0x5a82799a) > 2:
            raise RuntimeError("CORDIC FUNC=1 sine mismatch: %#x" % words[6])
        if not (words[7] & (1 << 30)) or words[8] != 0:
            raise RuntimeError("CORDIC unsupported config behavior mismatch: %r" % words[7:9])
        if words[9] != 0:
            raise RuntimeError("CORDIC reset CSR did not clear diagnostic: %#x" % words[9])
        print("RESULT: STM32H723 CORDIC bare-metal smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("CORDIC result was not observed: %r" % words)
command("quit")
sock.close()
PY
