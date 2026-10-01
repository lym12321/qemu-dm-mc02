#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-v2-step-done.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/readback.elf"
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
    -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$cosim_socket" && -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$cosim_socket" && -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$cosim_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time

COSIM, QMP = sys.argv[1:]
MAGIC = 0x32434D44
RESET, STEP, STEP_ACK, RESET_ACK, STEP_DONE = 1, 2, 3, 5, 6
HEADER = 36
MARKER = 0x52424B31


def exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("socket closed")
        result.extend(chunk)
    return bytes(result)


def read_frame(sock):
    body_len = struct.unpack("<I", exact(sock, 4))[0]
    return exact(sock, body_len)


def read_v2(sock, wanted_kind=None):
    while True:
        body = read_frame(sock)
        if (len(body) >= 8 and struct.unpack_from("<H", body, 4)[0] == 2 and
                (wanted_kind is None or
                 struct.unpack_from("<H", body, 6)[0] == wanted_kind)):
            return body


def v2_frame(kind, step_id, timestamp, payload=b"", dt=0):
    body = struct.pack("<IHHIQQQI", MAGIC, 2, kind, len(payload),
                       step_id, timestamp, dt, 0) + payload
    return struct.pack("<I", len(body)) + body


def fields(body):
    return (struct.unpack_from("<H", body, 6)[0],
            struct.unpack_from("<Q", body, 12)[0])


def payload(body):
    length = struct.unpack_from("<I", body, 8)[0]
    return body[4 + HEADER:4 + HEADER + length]


cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.settimeout(3.0)
cosim.connect(COSIM)
read_frame(cosim)  # initial v1 telemetry
cosim.sendall(v2_frame(RESET, 0, 0))
if fields(read_v2(cosim)) != (RESET_ACK, 0):
    raise RuntimeError("v2 RESET_ACK mismatch")

qmp = QmpSession(QMP, timeout=2.0)

# Wait until the guest has configured both BMI088 dies and is ready to read the
# next accepted sample. The marker is in DTCM and is observable through QMP.
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    response = qmp.command("human-monitor-command", {
        "command-line": "xp /8wx 0x20000000"}) or ""
    words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", response)]
    if len(words) >= 8 and words[7] == 0x434F4E46:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not finish BMI088 configuration")

step_wire = v2_frame(
    STEP, 7, 1, struct.pack("<12f", 1.0, -2.0, 3.0,
                              0.5, -1.0, 1.0,
                              0.0, 0.0, 0.0, 0.0, 0.0, 0.0) +
    struct.pack("<QI", 1, 1), dt=1)
cosim.sendall(step_wire)

ack = read_v2(cosim, STEP_ACK)
if fields(ack) != (STEP_ACK, 7) or struct.unpack_from("<I", payload(ack))[0] != 0:
    raise RuntimeError("STEP_ACK was not successful: %r status=%d" %
                       (fields(ack), struct.unpack_from("<I", payload(ack))[0]))

done = read_v2(cosim, STEP_DONE)
if fields(done) != (STEP_DONE, 7):
    raise RuntimeError("STEP_DONE step identity mismatch")
done_values = struct.unpack("<IIIII", payload(done))
if done_values != (0, 3, 0, 0, 0):
    raise RuntimeError("unexpected STEP_DONE payload: %r" % (done_values,))

# Replaying an already consumed STEP is the recovery path when STEP_DONE was
# lost. It must return both phases without another guest sensor transaction.
cosim.sendall(step_wire)
retry_ack = read_v2(cosim, STEP_ACK)
retry_done = read_v2(cosim, STEP_DONE)
if fields(retry_ack) != (STEP_ACK, 7) or fields(retry_done) != (STEP_DONE, 7):
    raise RuntimeError("consumed STEP was not replayed idempotently")

response = qmp.command("human-monitor-command", {
    "command-line": "xp /13wx 0x20000000"}) or ""
words = [int(value, 16) for value in re.findall(
    r"0x([0-9a-fA-F]{8})", response)]
if not words or words[0] != MARKER or words[10] != 0 or words[11] != 0:
    raise RuntimeError("guest did not consume both BMI088 raw bursts")
print("RESULT: QEMU v2 STEP_DONE guest BMI088 consumption smoke passed")
PY
