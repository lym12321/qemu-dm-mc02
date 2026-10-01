#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'RESULT: blocked (arm-none-eabi-gcc is required)' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'RESULT: blocked (python3 is required)' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'RESULT: blocked (QEMU not found: %s)\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-clock-zero.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/clock-zero.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_pwr_rcc_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_clock_zero_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -S \
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

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def read_words():
    text = command("human-monitor-command", {"command-line": "xp /3wx 0x20000000"}) or ""
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]

command("cont")
words = []
for _ in range(10000):
    words = read_words()
    if len(words) >= 3 and words[1] == 3:
        break
else:
    raise RuntimeError("first state: RCC request was not committed: %r" % words)
if words[0] != 0x434c5a30:
    raise RuntimeError("first state: guest setup marker missing: %r" % words)

timer_clock = command("qom-get", {"path": "/machine", "property": "apb1-timer-clock-hz"})
if timer_clock != 0:
    raise RuntimeError("first state: APB1 timer clock remained stale: %r, guest=%r" %
                       (timer_clock, words))
print("RESULT: DM-MC02 zero effective system-clock smoke passed")
print("  guest RCC CFGR/CR: 0x%08x 0x%08x" % (words[1], words[2]))
print("  APB1 timer clock: %d Hz" % timer_clock)
command("quit")
sock.close()
PY
