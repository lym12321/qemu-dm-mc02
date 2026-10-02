#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
elf="$root_dir/build/smoke/dm_mc02_bmi088_filter_smoke.elf"

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 1
}
mkdir -p "$root_dir/build/smoke"
if [[ ! -x "$elf" ||
      "$root_dir/smoke/dm_mc02_bmi088_filter_smoke.c" -nt "$elf" ||
      "$root_dir/smoke/dm_mc02_bmi088_filter_smoke.ld" -nt "$elf" ]]; then
    arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
        -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
        -Wl,--build-id=none \
        -Wl,-T,"$root_dir/smoke/dm_mc02_bmi088_filter_smoke.ld" \
        -o "$elf" "$root_dir/smoke/dm_mc02_bmi088_filter_smoke.c"
fi

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket chardev --socket qmp \
    --python-arg "{chardev}" --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$elf" -nodefaults -display none \
    -monitor none -S \
    -chardev "socket,id=cosim,path={chardev},server=on,wait=off" \
    -serial chardev:cosim \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time

CHARDEV, QMP = sys.argv[1:]
MAGIC = 0x32434D44
VERSION = 1
HEADER = 28
RESET, IMU = 1, 2

def frame(kind, sequence, timestamp, payload=b""):
    body = struct.pack("<IHHIQQ", MAGIC, VERSION, kind, len(payload),
                       sequence, timestamp) + payload
    return struct.pack("<I", len(body)) + body

def recv_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        data = sock.recv(size - len(result))
        if not data:
            raise RuntimeError("chardev closed")
        result.extend(data)
    return bytes(result)

chardev = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
chardev.settimeout(2.0)
chardev.connect(CHARDEV)
outer = struct.unpack("<I", recv_exact(chardev, 4))[0]
recv_exact(chardev, outer)  # initial telemetry
chardev.sendall(frame(RESET, 0, 0))

qmp = QmpSession(QMP, timeout=2.0)
qmp_buf = bytearray()

command = qmp.command

command("cont")

def words():
    text = command("human-monitor-command", {
        "command-line": "xp /20wx 0x20000000",
    })
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text or "")]

def wait_for(predicate, description):
    deadline = time.monotonic() + 3.0
    latest = []
    while time.monotonic() < deadline:
        latest = words()
        if predicate(latest):
            return latest
        time.sleep(0.002)
    raise RuntimeError(f"timeout waiting for {description}: {latest!r}")

wait_for(lambda value: len(value) >= 2 and value[0] == 0x46494c54 and
         value[1] == 1, "high-bandwidth configuration")

def send_sample(sequence, timestamp, gyro_x):
    chardev.sendall(frame(IMU, sequence, timestamp,
                          struct.pack("<6f", gyro_x, 0.0, 0.0,
                                      0.0, 0.0, 1.0)))

sequence = 1
send_sample(sequence, 1_000_000, 0.0)
sequence += 1
for index in range(1, 8):
    wait_for(lambda value, index=index: len(value) >= 4 and
             value[3] >= index, f"high sample {index}")
    send_sample(sequence, (index + 1) * 1_000_000, 1000.0)
    sequence += 1

wait_for(lambda value: len(value) >= 2 and value[1] == 2,
         "low-bandwidth configuration")
send_sample(sequence, 20_000_000, 0.0)
sequence += 1
for sample_index in range(1, 8):
    wait_for(lambda value, sample_index=sample_index: len(value) >= 4 and
             value[3] >= 8 + sample_index, f"low sample {sample_index}")
    send_sample(sequence, 20_000_000 + sample_index * 5_000_000,
                1000.0)
    sequence += 1

value = wait_for(lambda value: len(value) >= 20 and value[1] == 3,
                 "filter smoke completion")
def signed16(item):
    item &= 0xffff
    return item - 0x10000 if item & 0x8000 else item

high = [signed16(item) for item in value[4:12]]
low = [signed16(item) for item in value[12:20]]
full_scale_raw = round(1000.0 * 32768.0 / 2000.0)
if high[0] != 0 or low[0] != 0:
    raise RuntimeError(f"filter did not initialize from zero: {high!r} {low!r}")
if not (0 < high[1] < full_scale_raw and high[5] > high[1]):
    raise RuntimeError(f"116 Hz step response is invalid: {high!r}")
if not (0 < low[1] < full_scale_raw and high[5] > low[1]):
    raise RuntimeError(f"23 Hz attenuation is invalid: {low!r}")
print("RESULT: BMI088 bandwidth/filter smoke passed")
print("  116 Hz response:", " ".join(map(str, high)))
print("  23 Hz response: ", " ".join(map(str, low)))
command("quit")
qmp.close()
chardev.close()
PY
