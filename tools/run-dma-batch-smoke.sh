#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-dma-batch.XXXXXX")
guest_elf="$run_dir/dma-batch.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_dma_batch_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_dma_batch_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket uart --socket qmp \
    --python-arg "{uart}" --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -chardev "socket,id=uart1,path={uart},server=on,wait=off" \
    -serial chardev:uart1 -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import sys
import time

uart_path, qmp_path = sys.argv[1:]
uart = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
uart.settimeout(0.05)
uart.connect(uart_path)
qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

def words(address, count=1):
    text = command("human-monitor-command", {
        "command-line": f"xp /{count}wx {address:#x}"
    }) or ""
    return [int(value, 16) for value in
            re.findall(r"0x([0-9a-fA-F]{8})", text)]

command("cont")

received = b""
deadline = time.monotonic() + 2.0
last = None
while time.monotonic() < deadline:
    try:
        received += uart.recv(256)
    except socket.timeout:
        pass
    result = words(0x20000000, 1)
    lisr = words(0x40020000, 1)
    regs = words(0x40020028, 4)
    last = (received, result, lisr, regs)
    if (result and result[0] == 0x42415431 and len(lisr) == 1 and
            len(regs) == 4 and len(received) >= 8):
        # The UART host endpoint must see real TDR writes in buffer order.
        if received[:8] != b"BATCBATC":
            raise RuntimeError(f"UART batch byte side effect mismatch: {received!r}")
        # Stream1 flags occupy bits 6..11: HTIF=bit10 and TCIF=bit11.
        if lisr[0] & ((1 << 10) | (1 << 11)) != ((1 << 10) | (1 << 11)):
            raise RuntimeError(f"DMA batch HT/TC flags missing: {lisr[0]:#x}")
        if regs[0] & 1 == 0 or regs[1] != 4:
            raise RuntimeError(f"DMA circular state mismatch: {regs!r}")
        if regs[2] != 0x40011028 or regs[3] != 0x20000100:
            raise RuntimeError(f"DMA endpoint/address mismatch: {regs!r}")
        print("RESULT: DMA batch UART side-effect/HT-TC/circular smoke passed")
        break
    time.sleep(0.001)
else:
    raise RuntimeError(f"DMA batch result was not observed: {last!r}")

command("quit")
qmp.close()
uart.close()
PY
