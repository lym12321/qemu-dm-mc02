#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-dma-fcr.XXXXXX")
guest_elf="$run_dir/dma-fcr.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_dma_fcr_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_dma_fcr_smoke.c"

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
uart.settimeout(0.02)
uart.connect(uart_path)
qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

def words(address, count):
    text = command("human-monitor-command", {
        "command-line": f"xp /{count}wx {address:#x}"
    }) or ""
    return [int(value, 16) for value in
            re.findall(r"0x([0-9a-fA-F]{8})", text)]

command("cont")
uart.sendall(b"AB")
intermediate = None
deadline = time.monotonic() + 1.0
while time.monotonic() < deadline:
    intermediate = words(0x40020054, 1)
    if intermediate and ((intermediate[0] >> 3) & 0x7):
        break
    time.sleep(0.005)
if not intermediate or ((intermediate[0] >> 3) & 0x7) != 1:
    raise RuntimeError(f"FIFO FS did not expose quarter occupancy: {intermediate!r}")
uart.sendall(b"CD")
received = b""
deadline = time.monotonic() + 2.0
last = None
while time.monotonic() < deadline:
    try:
        received += uart.recv(256)
    except socket.timeout:
        pass
    result = words(0x20000000, 24)
    last = (received, result)
    if len(result) >= 24 and result[0] == 0x46435231:
        if result[1] != 0x87 or result[2] != 0x85:
            raise RuntimeError(f"FCR writable/FS/reserved mismatch: {result[1:3]!r}")
        if result[3] & 0x7 != 0x4:
            raise RuntimeError(f"direct-mode DMEIF missing: {result[3]:#x}")
        if result[4] & 1:
            raise RuntimeError(f"direct mismatch did not clear EN: {result[4]:#x}")
        if result[5] != 0 or result[6] & 1:
            raise RuntimeError(f"FIFO transfer did not complete: {result[5:7]!r}")
        if result[3] & 0xffc0 != 0xc00:
            raise RuntimeError(f"FIFO HTIF/TCIF missing: {result[3]:#x}")
        if result[7] != 0:
            raise RuntimeError("DMEIE gate was not effective before enable")
        if result[10] != 0x49525146 or result[11] & 0x7 != 0x4:
            raise RuntimeError(f"DMEIE IRQ did not observe DMEIF: {result[10:12]!r}")
        if result[12] & 0x7:
            raise RuntimeError(f"DMEIF W1C failed: {result[12]:#x}")
        tdr = words(0x40011028, 1)
        if not tdr or tdr[0] != 0x42:
            raise RuntimeError(f"FIFO width conversion TDR mismatch: {tdr!r}, result={result!r}")
        if result[15] != 4 or not (result[16] & 1) or not (result[16] & 0x100):
            raise RuntimeError(f"P2M FIFO circular reload failed: {result[15:17]!r}")
        if result[14] & 0x300000 != 0x300000:
            raise RuntimeError(f"P2M FIFO HTIF/TCIF missing: {result[14]:#x}")
        if ((result[17] >> 3) & 0x7) != 0 or result[18:20] != [0x44434241, 0]:
            raise RuntimeError(f"P2M FIFO packing mismatch: {result[17:20]!r}")
        print("RESULT: DMA FCR/direct-error/FIFO-width smoke passed")
        break
    time.sleep(0.005)
else:
    raise RuntimeError(f"DMA FCR result was not observed: {last!r}")

command("quit")
qmp.close()
uart.close()
PY
