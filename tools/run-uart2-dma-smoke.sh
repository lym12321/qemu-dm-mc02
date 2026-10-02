#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
uart_dma_endpoint=${1:-on}
case "$uart_dma_endpoint" in
    on|off) ;;
    *)
        printf 'usage: %s [on|off]\n' "${BASH_SOURCE[0]}" >&2
        exit 2
        ;;
esac
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-uart2-dma.XXXXXX")
guest_elf="$run_dir/uart2-dma.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_uart_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_uart2_dma_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket uart --socket qmp \
    --python-arg "{uart}" --python-arg "{qmp}" --python-arg "$uart_dma_endpoint" -- \
    "$qemu_bin" -machine "dm-mc02,uart-dma-endpoint=$uart_dma_endpoint" \
    -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S -serial none -serial none \
    -chardev "socket,id=uart2,path={uart},server=on,wait=off" \
    -serial chardev:uart2 -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import sys
import time

uart_path, qmp_path, endpoint_mode = sys.argv[1:]
uart = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
uart.settimeout(0.05)
uart.connect(uart_path)
qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

command("cont")
if uart.recv(1) != b"P":
    raise RuntimeError("USART2 polling preamble mismatch")
uart.sendall(b"Z")
tx = b""
deadline = time.monotonic() + 2.0
while len(tx) < 2 and time.monotonic() < deadline:
    try:
        tx += uart.recv(2 - len(tx))
    except socket.timeout:
        pass
if tx != b"D2":
    raise RuntimeError(f"USART2 DMA TX mismatch: {tx!r}")

last = []
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /6wx 0x20000000"})
    last = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(last) >= 6 and last[5] == 1:
        if last[1] != ord("Z") or last[2:4] != [0, 0]:
            raise RuntimeError(f"USART2 DMA result mismatch: {last!r}")
        if last[4] != 0:
            raise RuntimeError(f"USART2 DMA address/base mismatch: {last!r}")
        print("RESULT: USART2 DMA1-RX/DMA2-TX smoke passed (%s)" % sys.argv[3])
        break
    time.sleep(0.01)
else:
    raise RuntimeError(f"USART2 DMA result was not observed: {last!r}")
command("quit")
qmp.close()
uart.close()
PY
