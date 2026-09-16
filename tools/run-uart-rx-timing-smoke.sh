#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-uart-rx-timing.XXXXXX")
qtest_socket="$run_dir/qtest.sock"
uart_socket="$run_dir/uart1.sock"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

"$qemu_bin" -machine dm-mc02 -nodefaults \
    -display none -monitor none \
    -accel qtest \
    -qtest "unix:$qtest_socket,server=on,wait=on" \
    -serial none \
    -chardev "socket,id=uart1,path=$uart_socket,server=on,wait=off" \
    -serial chardev:uart1 \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qtest_socket" ]] && break
    sleep 0.01
done
[[ -S "$qtest_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$qtest_socket" "$uart_socket" <<'PY'
import select
import socket
import sys
import time

QTEST_SOCKET, UART_SOCKET = sys.argv[1:]
USART1_CR1 = 0x40011000
USART1_BRR = 0x4001100C
USART1_ISR = 0x4001101C
USART1_ICR = 0x40011020
USART1_RDR = 0x40011024
USART1_PRESC = 0x4001102C
CR1_RE = 1 << 2
ISR_IDLE = 1 << 4
ISR_RXNE = 1 << 5
ICR_IDLECF = 1 << 4
FRAME_NS = 1_000_000


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


def step(nanoseconds):
    response = command("clock_step %d" % nanoseconds)
    if not response.startswith(b"OK "):
        raise RuntimeError("clock_step did not return a timestamp: %r" %
                           response)


def expect_rdr(expected):
    value = read_reg(USART1_RDR) & 0xff
    if value != expected:
        raise RuntimeError("RDR mismatch: 0x%x != 0x%x" % (value, expected))


def expect_flags(rxne, idle):
    status = read_reg(USART1_ISR)
    if bool(status & ISR_RXNE) != rxne:
        raise RuntimeError("RXNE mismatch: 0x%x" % status)
    if bool(status & ISR_IDLE) != idle:
        raise RuntimeError("IDLE mismatch: 0x%x" % status)


def send_input(data):
    uart.sendall(data)
    # Let the QEMU main loop consume the host socket before virtual time is
    # advanced.  This is host I/O synchronization, not timing simulation.
    time.sleep(0.01)


# The reset USART1 kernel clock is 64 MHz.  BRR=6400 and PRESC=/1 yield
# exactly one virtual millisecond for a default 8N1 frame.
command("writel 0x%x 0x%x" % (USART1_CR1, CR1_RE))
command("writel 0x%x 0" % USART1_PRESC)
command("writel 0x%x 6400" % USART1_BRR)
expect_flags(False, False)

send_input(b"A")
step(FRAME_NS - 1)
expect_flags(False, False)
step(1)
expect_flags(True, False)
expect_rdr(ord("A"))
expect_flags(False, False)
step(FRAME_NS - 1)
expect_flags(False, False)
step(1)
expect_flags(False, True)
command("writel 0x%x 0x%x" % (USART1_ICR, ICR_IDLECF))
expect_flags(False, False)

# A host chunk is a burst on the wire, not a character boundary.  Each byte
# must become visible at one-frame intervals and the idle deadline starts only
# after the final byte has been delivered.
send_input(b"BC")
step(FRAME_NS - 1)
expect_flags(False, False)
step(1)
expect_flags(True, False)
expect_rdr(ord("B"))
step(FRAME_NS - 1)
expect_flags(False, False)
step(1)
expect_flags(True, False)
expect_rdr(ord("C"))
step(FRAME_NS)
expect_flags(False, True)

print("RESULT: UART RX virtual-time and IDLE smoke passed")
qtest.close()
uart.close()
PY
