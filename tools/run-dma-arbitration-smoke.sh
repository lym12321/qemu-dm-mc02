#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-dma-arbitration.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
uart_socket="$run_dir/uart1.sock"
guest_elf="$run_dir/dma-arbitration.elf"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_dma_arbitration_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_dma_arbitration_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S -serial none \
    -chardev "socket,id=uart1,path=$uart_socket,server=on,wait=off" \
    -serial chardev:uart1 -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$uart_socket" && -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$uart_socket" && -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$uart_socket" "$qmp_socket" <<'PY'
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

received = bytearray()

def read_uart():
    try:
        received.extend(uart.recv(256))
    except socket.timeout:
        pass

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    read_uart()
    result = words(0x20000000, 8)
    if result and result[0] == 0x41524231:
        if bytes(received[:4]) != b"CDAB":
            raise RuntimeError(
                f"DMA priority/batch arbitration mismatch: {bytes(received)!r}")
        break
    time.sleep(0.001)
else:
    raise RuntimeError(f"DMA TX arbitration marker was not observed: {result!r}")

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    result = words(0x20000000, 8)
    if len(result) >= 2 and result[1] == 0x41524232:
        uart.sendall(b"wxyz")
        break
    time.sleep(0.001)
else:
    raise RuntimeError(f"DMA RX arbitration marker was not observed: {result!r}")

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    result = words(0x20000000, 8)
    if len(result) >= 5 and result[2] == 0x41524233:
        # Stream 1 has the higher PL and must consume w/x first. Stream 0
        # takes over only after stream 1 reaches NDTR zero.
        if result[3] != 0x7a79 or result[4] != 0x7877:
            raise RuntimeError(f"DMA request arbitration mismatch: {result!r}")
        print("RESULT: DMA stream priority arbitration smoke passed")
        break
    time.sleep(0.001)
else:
    raise RuntimeError(f"DMA RX arbitration result was not observed: {result!r}")

command("quit")
qmp.close()
uart.close()
PY
