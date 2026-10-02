#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-ospi.XXXXXX")
guest_elf="$run_dir/ospi.elf"
flash_file="$run_dir/ospi.flash"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_ospi_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_ospi_smoke.c"

python3 - "$flash_file" <<'PY'
import sys

image = bytearray(b"\xff" * (8 * 1024 * 1024))
image[0x100:0x108] = bytes.fromhex("4433221188776655")
with open(sys.argv[1], "wb") as stream:
    stream.write(image)
PY

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 --expect-qemu-quit \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine "dm-mc02,ospi2-flash-file=$flash_file" \
    -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

deadline = time.monotonic() + 3.0
last = []
while time.monotonic() < deadline:
    text = command("human-monitor-command",
                   {"command-line": "xp /36wx 0x20000000"})
    last = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(last) >= 36 and last[0] == 0x4f535032 and last[16] == 1:
        if last[33:35] != [0x11223344, 0x55667788]:
            raise RuntimeError("OSPI persistence load mismatch: %r" % last[33:35])
        expected = [0x001740ef, 0x02,
                    0x11223344, 0x55667788, 0x99aabbcc, 0xddeeff00,
                    0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff,
                    0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff]
        if last[1:15] != expected:
            raise RuntimeError("OSPI smoke mismatch: %r" % last[:17])
        if last[35] != 0:
            raise RuntimeError("page program did not clear WEL: %r" % last[35])
        if last[15] & 0x02:
            raise RuntimeError("WEL remained set after rejected program: %r" % last[15])
        if last[17:19] != [0xffffffff, 0xffffffff]:
            raise RuntimeError("oversized page program modified Flash: %r" % last[:20])
        if last[19] != 0x02:
            raise RuntimeError("oversized page program lost WEL unexpectedly: %r" % last[:20])
        if not (last[20] & 0x01):
            raise RuntimeError("maximum DLR page program did not set TEF: %r" % last[:22])
        if last[21] != 0x02:
            raise RuntimeError("maximum DLR page program lost WEL: %r" % last[:22])
        if not (last[22] & 0x01):
            raise RuntimeError("maximum DLR read did not set TEF: %r" % last[:24])
        if last[23] != 0x02:
            raise RuntimeError("maximum DLR read changed WEL: %r" % last[:24])
        if last[24] != 0x11223344 or last[25] != 0xffffffff:
            raise RuntimeError("32 KiB erase mismatch: %r" % last[24:26])
        if last[26] != 0x11223344 or last[27] != 0xffffffff:
            raise RuntimeError("64 KiB erase mismatch: %r" % last[26:28])
        if last[28] != 0x11223344 or last[29] != 0xffffffff:
            raise RuntimeError("chip erase mismatch: %r" % last[28:30])
        if last[30] != 0:
            raise RuntimeError("chip erase left Flash status set: %r" % last[30])
        if last[31] != 0xffffffff or last[32] != 0:
            raise RuntimeError("chip erase depended on stale DLR: %r" % last[31:33])
        print("RESULT: W25Q64 OCTOSPI2 bare-metal smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("OSPI smoke result was not observed: %r" % last)
command("quit")
sock.close()
PY
python3 - "$flash_file" <<'PY'
import os
import sys

path = sys.argv[1]
assert os.path.getsize(path) == 8 * 1024 * 1024
with open(path, "rb") as stream:
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        assert chunk == b"\xff" * len(chunk)
print("RESULT: W25Q64 OCTOSPI2 persistence load/save passed")
PY
