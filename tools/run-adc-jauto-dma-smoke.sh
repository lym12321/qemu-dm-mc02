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

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-jauto-dma.XXXXXX")
guest_elf="$run_dir/adc-jauto-dma.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_adc_jauto_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_jauto_dma_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 10 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02,adc-accurate-timing=on \
    -kernel "$guest_elf" -nodefaults -display none -monitor none \
    -serial none -d guest_errors -D /tmp/dm-mc02-adc-jauto-dma.log \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)
buf = bytearray()

command = sock.command

def words(address, count):
    output = command("human-monitor-command", {
        "command-line": f"xp /{count}wx 0x{address:x}"
    }) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", output)]

deadline = time.monotonic() + 5.0
last = None
while time.monotonic() < deadline:
    marker = words(0x20000000, 1)
    if marker != [0x4A444D31]:
        time.sleep(0.001)
        continue
    dma_lisr = words(0x40020000, 1)
    dma_cr = words(0x40020040, 1)
    dma_ndtr = words(0x40020044, 1)
    adc = words(0x40022000, 1)
    cr = words(0x40022008, 1)
    jsqr = words(0x4002204C, 1)
    jdr1 = words(0x40022080, 1)
    jdr2 = words(0x40022084, 1)
    samples = words(0x20000100, 2)
    last = ((dma_lisr, dma_cr, dma_ndtr), adc, cr, jsqr, jdr1, jdr2,
            samples)
    if (len(dma_lisr) >= 1 and len(dma_cr) >= 1 and
            len(dma_ndtr) >= 1 and len(adc) >= 1 and len(cr) >= 1 and
            len(jsqr) >= 1 and len(jdr1) >= 1 and len(jdr2) >= 1 and
            len(samples) >= 2):
        if ((adc[0] & ((1 << 5) | (1 << 6))) == ((1 << 5) | (1 << 6)) and
                (cr[0] & (1 << 3)) and jsqr[0] == 0 and
                jdr1[0] == 0x0100 and jdr2[0] == 0x0200 and
                (dma_cr[0] & ((1 << 0) | (1 << 8) | (1 << 10))) ==
                    ((1 << 0) | (1 << 8) | (1 << 10)) and
                1 <= (dma_ndtr[0] & 0xffff) <= 4 and
                (dma_lisr[0] & ((1 << 20) | (1 << 21))) ==
                    ((1 << 20) | (1 << 21)) and
                samples == [0x02000100, 0x02000100]):
            command("quit")
            print("RESULT: ADC JAUTO+JQM regular DMA smoke passed")
            print("  regular DMA resumed after automatic injected context emptied")
            break
    time.sleep(0.001)
else:
    raise RuntimeError("JAUTO+JQM DMA state was not observed: %r" % (last,))
sock.close()
PY
