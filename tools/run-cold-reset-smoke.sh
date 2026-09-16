#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
run_dir=$(mktemp -d "$root_dir/output.dm-mc02-cold-reset.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/cold-reset.elf"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_cold_reset_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cold_reset_smoke.c"

"$qemu_bin" -machine dm-mc02,cold-reset=on -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" >/dev/null \
    2>"$run_dir/qemu.stderr" &
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

def read_words():
    text = command("human-monitor-command",
                   {"command-line": "xp /2wx 0x20000000"})
    return [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    words = read_words()
    if words[:1] == [0x434f4c44]:
        break
    time.sleep(0.01)
else:
    raise RuntimeError(f"cold-reset initial marker not observed: {words!r}")

command("system_reset")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    words = read_words()
    if words[:1] == [0x434f4c44]:
        if words[:2] != [0x434f4c44, 0]:
            raise RuntimeError(f"cold-reset SRAM mismatch: {words!r}")
        print("RESULT: cold-reset clears volatile SRAM while preserving boot")
        break
    time.sleep(0.01)
else:
    raise RuntimeError(f"cold-reset did not clear SRAM: {words!r}")
command("quit")
PY
