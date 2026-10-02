#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-tim8-approx-dma.XXXXXX")
guest_elf="$run_dir/tim8-approx-dma.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_tim8_approx_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_tim8_approx_dma_smoke.c"

# Use the default timing mode: this exercises the coalesced path.

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -d guest_errors \
    -D /tmp/dm-mc02-tim8-approx-dma.log \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def word(address):
    text = command("human-monitor-command", {
        "command-line": f"xp /1wx {address:#x}"
    }) or ""
    values = re.findall(r"0x([0-9a-fA-F]{8})", text)
    return int(values[0], 16) if values else None

deadline = time.monotonic() + 5.0
last = None
while time.monotonic() < deadline:
    marker = word(0x20000000)
    ccr = word(0x40010434)
    last = (marker, ccr)
    if marker == 0x54414D31 and ccr == 44:
        break
    # QMP and the TCG main loop share the same scheduling budget on this
    # build.  Leave a meaningful run interval between observations so the
    # virtual timer can deliver the coalesced batch under host load.
    time.sleep(0.020)
else:
    raise RuntimeError(f"TIM8 approximate DMA result was not observed: {last}")

ndtr = word(0x400204a4)
cr = word(0x400204a0)
m0ar = word(0x400204ac)
hisr = word(0x40020404)
last = (last[0], last[1], ndtr, cr, m0ar, hisr)
if None in last:
    raise RuntimeError(f"TIM8 approximate DMA registers were not observed: {last}")
if ndtr != 4:
    raise RuntimeError(f"NDTR was not circularly reloaded: {ndtr}")
expected_cr = (1 << 0) | (1 << 6) | (1 << 8) | (1 << 10) | (1 << 11) | (1 << 13)
if cr != expected_cr:
    raise RuntimeError(f"unexpected DMA2 Stream6 CR: {cr:#x}")
if m0ar != 0x20000100:
    raise RuntimeError(f"unexpected waveform M0AR: {m0ar:#x}")
if hisr & ((1 << 20) | (1 << 21)) != ((1 << 20) | (1 << 21)):
    raise RuntimeError(f"TIM8 DMA HT/TC flags missing: {hisr:#x}")
print("RESULT: TIM8 approximate coalesced DMA smoke passed")
command("quit")
sock.close()
PY
