#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-uart-idle.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
uart_socket="$run_dir/uart1.sock"
guest_elf="$run_dir/uart-idle.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_uart_idle_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_uart_idle_smoke.c"

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
[[ -S "$uart_socket" && -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$uart_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import sys
import time

uart_path, qmp_path = sys.argv[1:]
uart = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
uart.settimeout(1.0)
uart.connect(uart_path)
qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

def words():
    result = command("human-monitor-command", {"command-line": "xp /4wx 0x20000000"})
    return [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", result or "")]

command("cont")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    current = words()
    if len(current) >= 4 and current[3] == 0x52454144:
        break
    time.sleep(0.01)
else:
    raise RuntimeError(f"UART idle guest did not start: {current!r}")

uart.sendall(b"I")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    current = words()
    if len(current) >= 4 and current[1] == 0x49444C45:
        if not (current[0] & (1 << 4)):
            raise RuntimeError(f"IDLE flag missing: {current!r}")
        if current[2] & (1 << 4):
            raise RuntimeError(f"IDLE clear failed: {current!r}")
        print("RESULT: USART1 IDLE IRQ smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError(f"UART idle IRQ result was not observed: {current!r}")
command("quit")
qmp.close()
uart.close()
PY
