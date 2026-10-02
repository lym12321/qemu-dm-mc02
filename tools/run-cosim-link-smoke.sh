#!/usr/bin/env bash
set -Eeuo pipefail

# Native chardev integration smoke. The socket is a temporary test artifact;
# no user-directory state is created. This exercises one QEMU process and one
# client only; it is not a migration/snapshot or LED/buzzer GPIO test.
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
elf="$root_dir/build/smoke/dm_mc02_bmi088_smoke.elf"

command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
[[ -x "$elf" ]] || "$script_dir/build-bmi088-smoke.sh" >/dev/null

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 10 --expect-qemu-quit \
    --socket chardev --socket qmp \
    --python-arg "{chardev}" --python-arg "{qmp}" --python-arg "{qemu_pid}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$elf" -nodefaults \
    -display none -monitor none \
    -chardev "socket,id=cosim,path={chardev},server=on,wait=off" \
    -serial chardev:cosim \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import os
import socket
import struct
import sys
import time

CHARDEV, QMP, PID = sys.argv[1], sys.argv[2], int(sys.argv[3])
MAGIC = 0x32434D44
VERSION = 1
HEADER = 28
RESET, IMU, TELEMETRY = 1, 2, 3

def frame(kind, sequence, virtual_time_ns, payload=b""):
    protocol = struct.pack("<IHHIQQ", MAGIC, VERSION, kind, len(payload),
                           sequence, virtual_time_ns) + payload
    return struct.pack("<I", len(protocol)) + protocol

def recv_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise RuntimeError("chardev closed before initial telemetry")
        result.extend(part)
    return bytes(result)

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(2.0)
sock.connect(CHARDEV)
outer_length = struct.unpack("<I", recv_exact(sock, 4))[0]
if not (HEADER <= outer_length <= HEADER + 128):
    raise RuntimeError("invalid initial outer frame length")
header = recv_exact(sock, HEADER)
magic, version, kind, payload_len, sequence, virtual_time_ns = struct.unpack(
    "<IHHIQQ", header)
if (outer_length != HEADER + payload_len or
        (magic, version, kind, payload_len) != (MAGIC, VERSION, TELEMETRY, 12)):
    raise RuntimeError("invalid initial telemetry header")
payload = recv_exact(sock, payload_len)
led_rgb, brightness, buzzer, board_flags, reserved = struct.unpack(
    "<IIBBH", payload)
if (led_rgb, brightness, buzzer, board_flags, reserved) != (0, 0, 0, 0, 0):
    raise RuntimeError("initial telemetry snapshot is not all zero")
if sequence == 0:
    raise RuntimeError("TX sequence did not start independently at one")
print("initial TELEMETRY: sequence=%d virtual_time_ns=%d" %
      (sequence, virtual_time_ns))

# Send frames in deliberately small chunks so callback parsing also covers
# partial header and payload delivery. The invalid outer length must be
# discarded before the following valid frame. The second IMU is after RESET,
# so sequence 1 is valid again in the new RX session.
bad_outer_length = struct.pack("<I", 20)
imu1 = frame(IMU, 1, 1_000_000,
             struct.pack("<6f", 1.25, -2.5, 3.75, 4.0, -5.0, 6.0))
reset = frame(RESET, 0, 0)
imu2 = frame(IMU, 1, 2_000_000,
             struct.pack("<6f", -7.0, 8.0, -9.0, 10.0, -11.0, 12.0))
for wire in (bad_outer_length, imu1, reset, imu2):
    for offset in range(0, len(wire), 7):
        sock.sendall(wire[offset:offset + 7])
        time.sleep(0.002)
time.sleep(0.1)

try:
    os.kill(PID, 0)
except OSError as exc:
    raise RuntimeError("QEMU stopped while processing IMU/RESET frames") from exc
print("IMU -> RESET -> new IMU accepted; QEMU is still running")

def recv_frame():
    length = struct.unpack("<I", recv_exact(sock, 4))[0]
    body = recv_exact(sock, length)
    header = struct.unpack_from("<IHHIQQ", body)
    payload = body[HEADER:]
    if header[0:2] != (MAGIC, VERSION) or header[3] != len(payload):
        raise RuntimeError("invalid outbound frame after reset: %r" % (header,))
    return header, payload

# Closing and reopening the chardev must also create a new outbound session.
sock.close()
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(2.0)
sock.connect(CHARDEV)
reconnect_header, _ = recv_frame()
if (reconnect_header[2], reconnect_header[3], reconnect_header[4]) != \
        (RESET, 0, 0):
    raise RuntimeError("missing RESET marker after chardev reconnect: %r" %
                       (reconnect_header,))
reconnect_telemetry, _ = recv_frame()
if (reconnect_telemetry[2], reconnect_telemetry[3], reconnect_telemetry[4]) != \
        (TELEMETRY, 12, 1):
    raise RuntimeError("missing telemetry after chardev reconnect: %r" %
                       (reconnect_telemetry,))
if reconnect_telemetry[5] <= reconnect_header[5]:
    raise RuntimeError("reconnect telemetry timestamp did not advance")
print("chardev reconnect -> outbound RESET -> telemetry session verified")

# A machine reset starts a new outbound session as well. Verify that the
# reset marker precedes the fresh telemetry snapshot on the same chardev.
qmp = QmpSession(QMP, timeout=2.0)

qmp_command = qmp.command

diagnostics = qmp_command("qom-get", {"path": "/machine",
                                      "property": "cosim-diagnostics"})
if not isinstance(diagnostics, str) or "rx_frames=" not in diagnostics or \
        "imu_dropped=" not in diagnostics:
    raise RuntimeError("missing co-sim diagnostics: %r" % diagnostics)
print("co-sim diagnostics exposed:", diagnostics)
qmp_command("system_reset")

reset_header, _ = recv_frame()
if (reset_header[2], reset_header[3], reset_header[4]) != (RESET, 0, 0):
    raise RuntimeError("missing outbound RESET session marker: %r" %
                       (reset_header,))
telemetry_header, _ = recv_frame()
if (telemetry_header[2], telemetry_header[3], telemetry_header[4]) != \
        (TELEMETRY, 12, 1):
    raise RuntimeError("missing fresh telemetry after RESET: %r" %
                       (telemetry_header,))
if telemetry_header[5] <= reset_header[5]:
    raise RuntimeError("telemetry timestamp did not advance after RESET")
print("QEMU reset -> outbound RESET -> telemetry session verified")

sock.close()
qmp_command("quit")
qmp.close()
qmp.close()
PY

printf '%s\n' 'RESULT: QEMU-native co-sim link smoke passed'
