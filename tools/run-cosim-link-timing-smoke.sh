#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: existing QEMU binary not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "${TMPDIR:-/tmp}/dm-mc02-cosim-timing.XXXXXX")
chardev_socket="$run_dir/cosim.sock"
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/timing.elf"
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
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cosim_timing_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path=$chardev_socket,server=on,wait=off" \
    -serial chardev:cosim \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

timeout 20s python3 - "$chardev_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time

CHARDEV, QMP = sys.argv[1:]
MAGIC, VERSION, HEADER = 0x32434D44, 1, 28
RESET, IMU, TELEMETRY = 1, 2, 3
MARKER = 0x54494D31

def frame(kind, sequence, timestamp, payload=b""):
    body = struct.pack("<IHHIQQ", MAGIC, VERSION, kind, len(payload),
                       sequence, timestamp) + payload
    return struct.pack("<I", len(body)) + body

def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise RuntimeError("socket closed")
        data.extend(chunk)
    return bytes(data)

for path in (CHARDEV, QMP):
    deadline = time.monotonic() + 3
    while not __import__('os').path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.01)

cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.settimeout(2)
cosim.connect(CHARDEV)
size = struct.unpack("<I", exact(cosim, 4))[0]
packet = exact(cosim, size)
kind = struct.unpack_from("<H", packet, 6)[0]
if kind != TELEMETRY:
    raise RuntimeError("initial telemetry was not received")

qmp = QmpSession(QMP, timeout=2)
buf = bytearray()
command = qmp.command

# Queue a sample before the guest is resumed. RESET creates the mapping
# host t=0 -> current QEMU virtual time; this sample is due exactly 1 ms later.
cosim.sendall(frame(RESET, 0, 0))
cosim.sendall(frame(IMU, 1, 1_000_000,
                    struct.pack("<6f", 100.0, 0.0, 0.0,
                                0.5, 0.0, 1.0)))
command("cont")
time.sleep(0.08)
memory = command("human-monitor-command",
                 {"command-line": "xp /5wx 0x20000000"})
words = [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", memory)]
if len(words) < 5 or words[0] != MARKER:
    raise RuntimeError("guest result was not observed: %r" % memory)
if words[1] != 0 or words[2] != 0:
    raise RuntimeError("future sample was visible before its timestamp: %r" % words[1:3])
def signed(word):
    return word - 0x10000 if word & 0x8000 else word
if abs(signed(words[3]) - 1638) > 1 or abs(signed(words[4]) - 5461) > 1:
    raise RuntimeError("scheduled IMU sample mismatch: %r" % words[3:5])
print("RESULT: co-sim IMU virtual-time scheduling smoke passed")
PY
