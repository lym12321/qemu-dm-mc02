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

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-irq.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/adc-irq.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_adc_irq_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_irq_smoke.c"

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
    printf '%s\n' 'RESULT: ADC IRQ smoke failed (QMP socket missing)' >&2
    exit 1
}

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def words(count):
    text = command("human-monitor-command", {
        "command-line": "xp /%dwx 0x20001000" % count}) or ""
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]

command("cont")
last = None
for _ in range(3000):
    last = words(21)
    if len(last) >= 21 and last[15] == 0x1c and last[13] == 0x444f4e45:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("ADC IRQ result was not observed: %r" % (last,))

if last[0] != 0x41495231:
    raise RuntimeError("marker mismatch: %r" % (last,))
if last[14] != 0x4 or last[15] != 0x1c:
    raise RuntimeError("IER enable mismatch: initial=%#x final=%#x" %
                       (last[14], last[15]))
if last[12] != 0x49525131:
    raise RuntimeError("IRQ18 vector was not reached: %#x" % last[12])
eoc, eos, ovr = 1 << 2, 1 << 3, 1 << 4
if not (last[2] & eoc) or (last[2] & (eos | ovr)):
    raise RuntimeError("first EOC status mismatch: %#x" % last[2])
if last[3] & eoc:
    raise RuntimeError("reading ADC_DR did not clear first EOC: %#x" % last[3])
if not (last[4] & eoc) or (last[4] & (eos | ovr)):
    raise RuntimeError("second EOC status mismatch: %#x" % last[4])
if last[5] & eoc:
    raise RuntimeError("reading ADC_DR did not clear second EOC: %#x" % last[5])
if not (last[6] & eos):
    raise RuntimeError("EOS status was not set: %#x" % last[6])
if last[7] & eos:
    raise RuntimeError("EOS W1C clear failed: %#x" % last[7])
if not (last[8] & eoc) or (last[8] & ovr):
    raise RuntimeError("continuous first EOC mismatch: %#x" % last[8])
if not (last[9] & ovr):
    raise RuntimeError("continuous no-DR sampling did not set OVR: %#x" % last[9])
if last[19] != 0:
    raise RuntimeError("default OVRMOD is not zero: %#x" % last[19])
if last[16] != last[20]:
    raise RuntimeError("default OVRMOD did not retain old ADC_DR: %#x != %#x" %
                       (last[16], last[20]))
if not (last[17] & ovr):
    raise RuntimeError("reading ADC_DR cleared OVR before ISR W1C: %#x" % last[17])
if last[18] & ovr:
    raise RuntimeError("OVR W1C clear failed: %#x" % last[18])
if last[11] & eos:
    raise RuntimeError("continuous EOS W1C clear failed: %#x" % last[11])

print("RESULT: ADC IRQ/EOC/EOS/OVR bare-metal smoke passed")
print("  IRQ18 vector and IER EOCIE/EOSIE/OVRIE verified")
print("  EOC: ADC_DR clear; EOS/OVR: ISR write-1-to-clear")
print("  continuous no-DR sampling: OVR asserted and cleared")
command("quit")
sock.close()
PY
