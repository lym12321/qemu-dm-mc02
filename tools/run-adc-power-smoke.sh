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

run_dir=$(mktemp -d "${TMPDIR:-/tmp}/dm-mc02-adc-power.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/adc-power.elf"
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
    -Wl,-T,"$root_dir/smoke/dm_mc02_adc_power_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_power_smoke.c"

"$qemu_bin" -machine dm-mc02,adc-power-model=on -kernel "$guest_elf" \
    -nodefaults -display none -monitor none -serial none -S \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    printf '%s\n' 'RESULT: ADC power smoke failed (QMP socket missing)' >&2
    exit 1
}

timeout 20s python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def words():
    text = command("human-monitor-command", {
        "command-line": "xp /15wx 0x20000000",
    }) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

command("cont")

last = None
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    last = words()
    if len(last) >= 15 and last[14] == 0x444F4E45:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("ADC power result was not observed: %r" % (last,))

if last[0] != 0x41445031:
    raise RuntimeError("marker mismatch: %r" % (last,))
if last[1] != (1 << 29) or last[2] != 0:
    raise RuntimeError("power-model reset mismatch: CR=%#x ISR=%#x" %
                       (last[1], last[2]))
if last[3] != (1 << 28) or last[4] != 0:
    raise RuntimeError("pre-regulator enable was not rejected: CR=%#x ISR=%#x" %
                       (last[3], last[4]))
if last[5] != ((1 << 28) | 1) or last[6] != 1:
    raise RuntimeError("regulator-ready enable mismatch: CR=%#x ISR=%#x" %
                       (last[5], last[6]))
if not (1 <= last[7] <= 200000):
    raise RuntimeError("invalid regulator poll count: %d" % last[7])
if last[8] != ((1 << 28) | 1 | (1 << 2)):
    raise RuntimeError("start did not latch: %#x" % last[8])
if not (last[9] & (1 << 2)) or not (last[9] & (1 << 3)):
    raise RuntimeError("single conversion flags mismatch: %#x" % last[9])
if last[10] != 0x0100:
    raise RuntimeError("ADC_DR sample mismatch: %#x" % last[10])
if (last[11] & (1 << 2)) or not (last[11] & (1 << 3)):
    raise RuntimeError("ADC_DR did not acknowledge EOC: %#x" % last[11])
if last[12] != (1 << 29) or last[13] != (1 << 3):
    raise RuntimeError("DEEPPWD shutdown mismatch: CR=%#x ISR=%#x" %
                       (last[12], last[13]))
print("RESULT: DM-MC02 ADC power MMIO smoke passed")
print("  reset -> regulator startup -> ADRDY -> EOC/DR -> DEEPPWD verified")
command("quit")
sock.close()
PY
