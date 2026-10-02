#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: existing QEMU binary not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "${TMPDIR:-/tmp}/dm-mc02-cosim-link-readback.XXXXXX")
guest_elf="$run_dir/readback.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 --expect-qemu-quit \
    --socket chardev --socket qmp \
    --python-arg "{chardev}" --python-arg "{qmp}" --python-arg "{qemu_pid}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path={chardev},server=on,wait=off" \
    -serial chardev:cosim \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import math
import os
import re
import socket
import struct
import sys
import time

CHARDEV, QMP, PID = sys.argv[1], sys.argv[2], int(sys.argv[3])
MAGIC = 0x32434D44
VERSION = 1
HEADER = 28
RESET, IMU, TELEMETRY = 1, 2, 3
MARKER = 0x52424B31

def frame(kind, sequence, virtual_time_ns, payload=b""):
    protocol = struct.pack("<IHHIQQ", MAGIC, VERSION, kind, len(payload),
                           sequence, virtual_time_ns) + payload
    return struct.pack("<I", len(protocol)) + protocol

def recv_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise RuntimeError("chardev closed unexpectedly")
        result.extend(part)
    return bytes(result)

def send_chunked(sock, wire):
    for offset in range(0, len(wire), 11):
        sock.sendall(wire[offset:offset + 11])
        time.sleep(0.002)

chardev = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
chardev.settimeout(2.0)
chardev.connect(CHARDEV)
outer_length = struct.unpack("<I", recv_exact(chardev, 4))[0]
if outer_length != HEADER + 12:
    raise RuntimeError("invalid initial telemetry outer length")
header = recv_exact(chardev, HEADER)
magic, version, kind, payload_len, sequence, virtual_time_ns = \
    struct.unpack("<IHHIQQ", header)
if (magic, version, kind, payload_len) != (MAGIC, VERSION, TELEMETRY, 12):
    raise RuntimeError("invalid initial telemetry header")
if struct.unpack("<IIBBH", recv_exact(chardev, payload_len)) != (0, 0, 0, 0, 0):
    raise RuntimeError("initial telemetry is not zeroed")
if sequence == 0:
    raise RuntimeError("initial telemetry sequence is not initialized")
print("initial TELEMETRY received")

qmp = QmpSession(QMP, timeout=2.0)
qmp_command = qmp.command
qmp_command("cont")

# Let the guest program the BMI088 range registers before publishing the
# sample. This makes the raw-sensitivity assertion independent of paused-start
# ordering.
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    configured = qmp_command(
        "human-monitor-command", {"command-line": "xp /8wx 0x20000000"})
    configured_words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", configured or "")]
    if len(configured_words) >= 8 and configured_words[7] == 0x434f4e46:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not finish BMI088 range configuration")
send_chunked(chardev, frame(IMU, 1, 1_000_000,
                            struct.pack("<6f", 1.0, -2.0, 3.0,
                                        0.5, -1.0, 1.0)))
# The guest configured both dies for 100 Hz. The second frame is inside the
# ODR interval and must be ignored; the third is the next accepted sample.
send_chunked(chardev, frame(IMU, 2, 2_000_000,
                            struct.pack("<6f", 7.0, 8.0, 9.0,
                                        2.0, 2.0, 2.0)))
send_chunked(chardev, frame(IMU, 3, 11_000_000,
                            struct.pack("<6f", 4.0, -5.0, 6.0,
                                        1.0, 0.25, -0.5)))
time.sleep(0.05)

# GPIO/timer writes above produce an output telemetry update.  There may be
# several intermediate GPIO notifications, so consume until the final state.
deadline = time.monotonic() + 2.0
output_telemetry = None
while time.monotonic() < deadline:
    chardev.settimeout(max(0.05, deadline - time.monotonic()))
    try:
        outer = struct.unpack("<I", recv_exact(chardev, 4))[0]
        packet = recv_exact(chardev, outer)
    except socket.timeout:
        break
    if outer < HEADER + 12:
        continue
    h = struct.unpack("<IHHIQQ", packet[:HEADER])
    if h[2] != TELEMETRY or h[3] != 12:
        continue
    payload = struct.unpack("<IIBBH", packet[HEADER:HEADER + 12])
    if payload == (0xffffff, 255, 1, 3, 0):
        output_telemetry = payload
        break
if output_telemetry is None:
    raise RuntimeError("board output telemetry update was not observed")
print("output TELEMETRY:", output_telemetry)

memory_text = qmp_command(
    "human-monitor-command", {"command-line": "xp /13wx 0x20000000"})
if not isinstance(memory_text, str):
    raise RuntimeError("unexpected xp response")
words = [int(value, 16) for value in re.findall(
    r"0x([0-9a-fA-F]{8})", memory_text)]
if len(words) < 13:
    raise RuntimeError("xp returned too few words: %r" % memory_text)
if words[0] != MARKER:
    raise RuntimeError("readback marker mismatch: 0x%08x" % words[0])

if words[8] != 0 or words[9] != 0x80:
    raise RuntimeError("BMI088 data-ready status timing mismatch: %r" %
                       words[8:10])
if words[10] != 0 or words[11] != 0:
    raise RuntimeError("BMI088 data-ready status was not consumed: %r" %
                       words[10:12])
if words[12] != 281:
    raise RuntimeError("BMI088 sensor time mismatch: %d" % words[12])

# The configured BMI088 bandwidth is part of this readback: the third sample
# is 10 ms after the first, so the SPI raw values are the deterministic
# first-order-filtered values rather than an unfiltered host sample.
expected = [75, -108, 141, 3784, -2827, 2300]
actual = []
for index, (word, wanted) in enumerate(zip(words[1:7], expected)):
    if word >> 16:
        raise RuntimeError("raw[%d] has nonzero upper bits: 0x%08x" %
                           (index, word))
    combined = (word & 0xff) | (((word >> 8) & 0xff) << 8)
    signed = combined - 0x10000 if combined & 0x8000 else combined
    if abs(signed - wanted) > 1:
        raise RuntimeError("raw[%d]=%d, expected %d +/- 1" %
                           (index, signed, wanted))
    actual.append(signed)
print("SPI readback raw:", " ".join(str(value) for value in actual))

# Bad outer/protocol/NaN frames are rejected. RESET then permits sequence 1.
send_chunked(chardev, struct.pack("<I", 20))
send_chunked(chardev, frame(99, 2, 2_000_000))
send_chunked(chardev, frame(IMU, 3, 3_000_000,
                            struct.pack("<6f", math.nan, -2.0, 3.0,
                                        0.5, -1.0, 1.0)))
send_chunked(chardev, frame(RESET, 0, 0))
send_chunked(chardev, frame(IMU, 1, 4_000_000,
                            struct.pack("<6f", -4.0, 5.0, -6.0,
                                        1.0, 0.25, -0.5)))
time.sleep(0.1)
try:
    os.kill(PID, 0)
except OSError as exc:
    raise RuntimeError("QEMU exited after bad/NaN/RESET frames") from exc
status = qmp_command("query-status")
if not isinstance(status, dict):
    raise RuntimeError("unexpected QMP status")
print("bad/NaN rejected; RESET -> second IMU accepted; QEMU status:",
      status.get("status"))
qmp_command("quit")
qmp.close()
chardev.close()
PY

printf '%s\n' 'RESULT: IMU->SPI readback co-sim smoke passed'
