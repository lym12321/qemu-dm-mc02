#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-fdcan-medium.XXXXXX")
guest_elf="$run_dir/fdcan-medium.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_fdcan_medium_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_fdcan_medium_smoke.c"

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

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

command("cont")
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /13wx 0x20000000"})
    words = [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 8 and words[7] == 0x4D454455:
        expected = [0x300 << 18, 0x11111111, 0x100 << 18, 0x22222222]
        if words[:4] != expected:
            raise RuntimeError("standard bus ordering mismatch: %r" % words)
        if words[6] & 0x7F:
            raise RuntimeError("sender received a loopback frame: %r" % words)
        if (words[8] & 0x3f) != 3:
            raise RuntimeError("unexpected initial TXFQS free level: %r" % words)
        if (words[9] & 0x3f) != 3 or (words[10] & 0x7) != 0:
            raise RuntimeError("standard bus did not complete TX immediately: %r" % words)
        if (words[11] & 0x3f) != 3:
            raise RuntimeError("TX FIFO status mismatch after completion: %r" % words)
        if words[12] & 0x7:
            raise RuntimeError("TXBRP did not clear after completion: %r" % words)
        print("RESULT: FDCAN1/FDCAN2 standard CAN bus smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("medium guest did not complete")
command("quit")
sock.close()
PY
