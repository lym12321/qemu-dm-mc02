#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'RESULT: blocked (arm-none-eabi-gcc is required)' >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'RESULT: blocked (python3 is required)' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || {
    printf 'RESULT: blocked (QEMU not found: %s)\n' "$qemu_bin" >&2
    exit 1
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-rs485.XXXXXX")
guest_elf="$run_dir/rs485.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_rs485_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_rs485_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket uart --socket qmp \
    --python-arg "{uart}" --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S -serial none -serial none \
    -chardev "socket,id=uart2,path={uart},server=on,wait=off" \
    -serial chardev:uart2 -qmp "unix:{qmp},server=on,wait=off" <<'PY'
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

def memory_words(count):
    text = command("human-monitor-command", {
        "command-line": "xp /%dwx 0x20000000" % count}) or ""
    return [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text)]

def wait_word(index, expected):
    last = []
    for _ in range(2000):
        last = memory_words(14)
        if len(last) > index and last[index] == expected:
            return last
        time.sleep(0.001)
    raise RuntimeError("guest marker[%d] did not reach %#x: %r" %
                       (index, expected, last))

def drain():
    result = bytearray()
    while True:
        try:
            part = uart.recv(256)
        except socket.timeout:
            return bytes(result)
        if not part:
            raise RuntimeError("USART2 chardev closed")
        result.extend(part)

try:
    command("cont")

    low = wait_word(4, 0x4c4f5731)
    low_tx = drain()
    if low_tx:
        raise RuntimeError("DE=0 polling TX escaped to chardev: %r" % low_tx)
    if low[1] != (1 << 14) or (low[1] & (1 << 15)):
        raise RuntimeError("CR3 DEM/DEP mismatch: %#x" % low[1])
    if (low[2] & (3 << 8)) != (1 << 8):
        raise RuntimeError("GPIOD PD4 MODER is not output: %#x" % low[2])
    if low[3] & (1 << 4):
        raise RuntimeError("DE was not low before first TX: %#x" % low[3])
    uart.sendall(b"1")

    high = wait_word(6, 0x48494731)
    high_tx = drain()
    if high_tx != b"H":
        raise RuntimeError("DE=1 polling TX mismatch: %r" % high_tx)
    if not (high[5] & (1 << 4)):
        raise RuntimeError("DE was not high for enabled TX: %#x" % high[5])
    uart.sendall(b"2")

    final = wait_word(7, 0x46494e31)
    final_tx = drain()
    if final_tx:
        raise RuntimeError("DE=0 second polling TX escaped: %r" % final_tx)
    if not len(final) > 8 or (final[8] & (1 << 4)):
        raise RuntimeError("DE did not return low: %#x" % final[8])
    uart.sendall(b"3")

    auto_config = wait_word(9, 0x41555431)
    auto = wait_word(10, 0x41545831)
    auto_tx = drain()
    if auto_tx != b"A":
        raise RuntimeError("AF automatic DE polling TX mismatch: %r" % auto_tx)
    if (auto_config[11] & (3 << 8)) != (2 << 8):
        raise RuntimeError("PD4 MODER is not AF mode: %#x" % auto_config[11])
    if ((auto_config[12] >> 16) & 0xf) != 7:
        raise RuntimeError("PD4 AFR0 is not AF7: %#x" % auto_config[12])
    if auto_config[13] & (1 << 4):
        raise RuntimeError("AF stage unexpectedly raised ODR.DE: %#x" %
                           auto_config[13])
    print("RESULT: RS485 DE manual and AF-automatic polling smoke passed")
    print("  manual GPIO DE low/high/low and AF7 automatic USART DE verified")
except Exception as exc:
    print("RESULT: RS485 DE polling smoke failed: %s" % exc, file=sys.stderr)
    print("  Core must implement manual GPIO and AF7 automatic USART2 DE gating.",
          file=sys.stderr)
    raise SystemExit(1)
finally:
    try:
        command("quit")
    except Exception:
        pass
    qmp.close()
    uart.close()
PY
