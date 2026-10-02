#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
if (( $# == 0 )); then
    for mode in on off; do
        bash "${BASH_SOURCE[0]}" "$mode"
    done
    exit 0
fi
adc_dma_endpoint=${1:-on}
case "$adc_dma_endpoint" in
    on|off) ;;
    *)
        printf 'usage: %s [on|off]\n' "${BASH_SOURCE[0]}" >&2
        exit 2
        ;;
esac
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-dma.XXXXXX")
guest_elf="$run_dir/adc-dma.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_adc_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_dma_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" --python-arg "$adc_dma_endpoint" -- \
    "$qemu_bin" -machine "dm-mc02,adc-dma-endpoint=$adc_dma_endpoint" \
    -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -d guest_errors \
    -D /tmp/dm-mc02-adc-dma.log \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def words(command_line, width=8):
    text = command("human-monitor-command", {"command-line": command_line}) or ""
    return [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{%d})" % width, text)]

deadline = time.monotonic() + 3.0
last = None
while time.monotonic() < deadline:
    result = words("xp /5wx 0x20000000")
    samples = words("xp /4hx 0x20000100", 4)
    status = words("xp /1wx 0x40020000")
    last = (result, samples, status)
    if (len(result) >= 5 and len(samples) >= 4 and len(status) >= 1 and
            result[0] == 0x41444331):
        if samples != [0x0100, 0x0200, 0x0100, 0x0200]:
            raise RuntimeError("ADC sample mismatch: %r" % (samples,))
        # With rank-level conversion timing the circular cursor may advance
        # while QMP is reading the snapshot.  Any non-zero position is valid;
        # The buffer contents and both HT/TC flags prove that a complete
        # circular sequence crossed its half and terminal boundaries.
        if not (1 <= result[1] <= 4) or not (result[2] & 1) or not (result[2] & (1 << 8)):
            raise RuntimeError("ADC circular stream not active: %r" % (result[1:3],))
        if (result[3] != 0x40022040 or
                not (0x20000100 <= result[4] <= 0x20000108) or
                result[4] & 1):
            raise RuntimeError("ADC DMA endpoint mismatch: %r" % (result[3:5],))
        if (status[0] & ((1 << 20) | (1 << 21))) != ((1 << 20) | (1 << 21)):
            raise RuntimeError("ADC DMA HT/TC flags missing: %#x" % status[0])
        print("RESULT: ADC1 DMA circular smoke passed (%s)" % sys.argv[2])
        break
    time.sleep(0.01)
else:
    raise RuntimeError("ADC DMA result was not observed: %r" % (last,))
command("quit")
sock.close()
PY
