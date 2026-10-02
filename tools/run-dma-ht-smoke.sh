#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-dma-ht.XXXXXX")
guest_elf="$run_dir/dma-ht.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_dma_ht_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_dma_ht_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -S \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

command("cont")
deadline = time.monotonic() + 2.0
last = None
while time.monotonic() < deadline:
    text = command("human-monitor-command", {
        "command-line": "xp /4wx 0x20000000"
    }) or ""
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text)]
    last = words
    if len(words) >= 4 and words[0] == 0x48544631:
        expected = (1 << 4) | (1 << 5)
        if words[1] == expected and words[2] == expected:
            print("RESULT: DMA half-transfer status smoke passed")
            break
    time.sleep(0.01)
else:
    raise RuntimeError("DMA HT result was not observed: %r" % last)
command("quit")
sock.close()
PY
