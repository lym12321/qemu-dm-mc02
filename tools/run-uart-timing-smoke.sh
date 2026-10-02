#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qtest --socket uart \
    --wait-socket qtest \
    --python-arg "{qtest}" --python-arg "{uart}" -- \
    "$qemu_bin" -machine dm-mc02 -nodefaults \
    -display none -monitor none \
    -accel qtest \
    -qtest "unix:{qtest},server=on,wait=on" \
    -serial none \
    -chardev "socket,id=uart1,path={uart},server=on,wait=off" \
    -serial chardev:uart1 <<'PY'
import select
import socket
import sys
import time

QTEST_SOCKET, UART_SOCKET = sys.argv[1:]
USART1_CR1 = 0x40011000
USART1_BRR = 0x4001100C
USART1_ISR = 0x4001101C
USART1_TDR = 0x40011028
USART1_PRESC = 0x4001102C
CR1_TE = 1 << 3
ISR_TC = 1 << 6
ISR_TXE = 1 << 7


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    for _ in range(300):
        try:
            sock.connect(path)
            return sock
        except ConnectionRefusedError:
            time.sleep(0.01)
    raise RuntimeError("could not connect to %s" % path)


def line(sock):
    result = bytearray()
    while not result.endswith(b"\n"):
        chunk = sock.recv(128)
        if not chunk:
            raise RuntimeError("qtest socket closed")
        result.extend(chunk)
    return bytes(result)


qtest = connect(QTEST_SOCKET)
uart = connect(UART_SOCKET)


def command(text):
    qtest.sendall((text + "\n").encode())
    response = line(qtest)
    if not response.startswith(b"OK"):
        raise RuntimeError("qtest %s failed: %r" % (text, response))
    return response


def read_reg(address):
    response = command("readl 0x%x" % address)
    return int(response.split()[1], 16)


def assert_no_output():
    readable, _, _ = select.select([uart], [], [], 0)
    if readable:
        data = uart.recv(64)
        if data:
            raise RuntimeError("UART output arrived too early: %r" % data)


def read_output(expected):
    result = bytearray()
    deadline = time.monotonic() + 1.0
    while len(result) < len(expected) and time.monotonic() < deadline:
        readable, _, _ = select.select([uart], [], [], 0.1)
        if readable:
            result.extend(uart.recv(len(expected) - len(result)))
    if bytes(result) != expected:
        raise RuntimeError("UART output mismatch: %r != %r" %
                           (bytes(result), expected))


def step(nanoseconds):
    response = command("clock_step %d" % nanoseconds)
    if not response.startswith(b"OK "):
        raise RuntimeError("clock_step did not return a timestamp: %r" %
                           response)


# The reset USART16 kernel clock is 64 MHz.  BRR=6400 with PRESC=/1 gives
# 10 kbaud, so a default 8N1 frame occupies exactly 1,000,000 virtual ns.
command("writel 0x%x 0x%x" % (USART1_CR1, CR1_TE))
command("writel 0x%x 0" % USART1_PRESC)
command("writel 0x%x 6400" % USART1_BRR)
command("writeb 0x%x 0x41" % USART1_TDR)
command("writeb 0x%x 0x42" % USART1_TDR)
assert_no_output()
if read_reg(USART1_ISR) & ISR_TC:
    raise RuntimeError("TC asserted while paced TX bytes were pending")

step(999_999)
assert_no_output()
if read_reg(USART1_ISR) & ISR_TC:
    raise RuntimeError("TC asserted before the first frame completed")

step(1)
read_output(b"A")
if read_reg(USART1_ISR) & ISR_TC:
    raise RuntimeError("TC asserted while the second frame was pending")

step(999_999)
assert_no_output()
step(1)
read_output(b"B")
status = read_reg(USART1_ISR)
if (status & (ISR_TC | ISR_TXE)) != (ISR_TC | ISR_TXE):
    raise RuntimeError("TX status did not settle after final frame: 0x%x" %
                       status)

print("RESULT: UART virtual-time frame pacing smoke passed")
qtest.close()
uart.close()
PY
