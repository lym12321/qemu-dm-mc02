#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-dma.XXXXXX")
guest_elf="$run_dir/dma.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_dma_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S -serial none \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

qmp_path = sys.argv[1]
qmp = QmpSession(qmp_path, timeout=2)

command = qmp.command

command("cont")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /14wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 14 and words[0] == 0x444d4131:
        expected = [0x11223344, 0x55667788, 0x99aabbcc, 0xddeeff00]
        if (words[1:5] != expected or words[5] != 0 or
                words[6] != 0x30 or words[7] & 1):
            time.sleep(0.01)
            continue
        if words[8] != 0x20000110 or words[9] != 0x20000200:
            raise RuntimeError("DMA stream 0 address/base mismatch: %r" % words[8:10])
        if words[10] != 0x00220011 or words[11] != 0x33:
            raise RuntimeError("DMA width conversion mismatch: %r" % words[10:12])
        if words[12] != 0xc30 or words[13] != 0x410:
            raise RuntimeError("DMA stream flags or clear mismatch: %r" % words[12:14])
        print("RESULT: DMA memory-to-memory smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("DMA result was not observed")
command("quit")
qmp.close()
PY
