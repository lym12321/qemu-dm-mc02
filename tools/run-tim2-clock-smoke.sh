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

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-tim2-clock.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/tim2-clock.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_tim2_clock_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_tim2_clock_smoke.c"

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
    printf '%s\n' 'RESULT: TIM2 clock smoke failed (QMP socket missing)' >&2
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
        "command-line": "xp /6wx 0x20000000"}) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

def read_word(address):
    text = command("human-monitor-command", {
        "command-line": "xp /1wx 0x%x" % address}) or ""
    values = re.findall(r"0x([0-9a-fA-F]{8})", text)
    if not values:
        raise RuntimeError("cannot read 0x%x: %r" % (address, text))
    return int(values[0], 16)

def write_word(address, value):
    # HMP `mw` uses the guest physical address, value and access size.
    command("human-monitor-command", {
        "command-line": "mw 0x%x 0x%x 4" % (address, value)})

command("cont")
switched = None
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    current = words()
    if len(current) >= 6 and current[0] == 0x53574954:
        switched = (time.monotonic(), current)
        break
    time.sleep(0.0002)
if switched is None:
    raise RuntimeError("RCC switch marker was not observed")

start, current = switched
if not (1900000 <= current[2] <= 2100000):
    raise RuntimeError("TIM2 CNT was not preserved across RCC switch: %r" %
                       (current,))

before_psc_write = read_word(0x40000024)
write_word(0x40000028, 0x3f)
time.sleep(0.010)
after_psc_write = read_word(0x40000024)
if after_psc_write < before_psc_write:
    raise RuntimeError("TIM2 CNT regressed across runtime PSC write: %x -> %x" %
                       (before_psc_write, after_psc_write))
if after_psc_write - before_psc_write < 1000:
    raise RuntimeError("TIM2 did not advance after runtime PSC write: %x -> %x" %
                       (before_psc_write, after_psc_write))
write_word(0x40000028, 0x1f)

while time.monotonic() < deadline:
    current = words()
    if len(current) >= 6 and current[5] == 0x444f4e45:
        break
    time.sleep(0.0002)
else:
    raise RuntimeError("TIM2 update was not observed: %r" % (current,))

elapsed_ms = (time.monotonic() - start) * 1000.0
if elapsed_ms > 420.0:
    raise RuntimeError("first post-switch update was too late: %.3f ms (%r)" %
                       (elapsed_ms, current))
if not (current[4] & 1):
    raise RuntimeError("TIM2 UIF was not latched: %r" % (current,))
print("RESULT: TIM2 dynamic clock first-period smoke passed")
print("  preserved CNT: %d" % switched[1][2])
print("  runtime PSC CNT: %d -> %d" % (before_psc_write, after_psc_write))
print("  first update wall delay: %.3f ms" % elapsed_ms)
command("quit")
sock.close()
PY
