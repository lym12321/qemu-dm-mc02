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

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-uart-rx-reservation.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
uart_socket="$run_dir/uart1.sock"
guest_elf="$run_dir/uart-rx-reservation.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_uart_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_uart_rx_reservation_smoke.c"

"$qemu_bin" -machine "dm-mc02,uart-dma-endpoint=on" \
    -kernel "$guest_elf" -nodefaults -display none -monitor none -S \
    -serial none \
    -chardev "socket,id=uart1,path=$uart_socket,server=on,wait=off" \
    -serial chardev:uart1 \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
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
uart.settimeout(2.0)
uart.connect(uart_path)
qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

def recv_until(expected):
    data = b""
    deadline = time.monotonic() + 2.0
    while expected not in data and time.monotonic() < deadline:
        try:
            data += uart.recv(64)
        except socket.timeout:
            # QEMU polls the host chardev while servicing a monitor memory
            # request when the guest is busy in a polling loop.
            command("human-monitor-command",
                    {"command-line": "xp /9wx 0x20000000"})
    if expected not in data:
        raise RuntimeError(f"UART output mismatch: {data!r}")
    return data

def read_words(count):
    text = command("human-monitor-command",
                   {"command-line": f"xp /{count}wx 0x20000000"})
    return [int(value, 16)
            for value in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]

command("cont")
if uart.recv(1) != b"R":
    raise RuntimeError("UART reservation setup marker mismatch")
uart.sendall(b"X")
if recv_until(b"F")[-1:] != b"F":
    raise RuntimeError("UART reservation failure marker mismatch")
if recv_until(b"D")[-1:] != b"D":
    raise RuntimeError("UART reservation retry marker mismatch")

words = read_words(9)
if len(words) < 9:
    raise RuntimeError(f"UART reservation result was truncated: {words!r}")
values = words[:9]
if values[0] != 0x444f4e45:
    raise RuntimeError(f"UART reservation completion mismatch: {values!r}")
if not (values[1] & (1 << 5)):
    raise RuntimeError(f"RXNE was not retained after failed destination: {values!r}")
if values[2] != 1 or (values[3] & 1) or not (values[4] & (1 << 3)):
    raise RuntimeError(f"DMA failure did not preserve source state: {values!r}")
if values[5] != ord("X") or values[6] != 0 or (values[7] & (1 << 5)):
    raise RuntimeError(f"DMA retry did not commit the source byte: {values!r}")
if values[8] & (1 << 3):
    raise RuntimeError(f"DMA transfer error remained latched: {values!r}")

print("RESULT: UART RX DMA reservation/retry smoke passed")
command("quit")
qmp.close()
uart.close()
PY
