#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-v2-motor.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
qemu_pid=''
cleanup() {
    local status=$?
    if (( status != 0 )) && [[ -f "$run_dir/qemu.stderr" ]]; then
        sed -n '1,120p' "$run_dir/qemu.stderr" >&2
    fi
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
    return "$status"
}
trap cleanup EXIT

"$qemu_bin" -machine dm-mc02,cosim-motor-loopback=on \
    -nodefaults -display none -monitor none -S \
    -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$cosim_socket" ]] && break
    sleep 0.01
done
[[ -S "$cosim_socket" ]] || exit 1

python3 - "$cosim_socket" <<'PY'
import math
import socket
import struct
import sys

path = sys.argv[1]
MAGIC = 0x32434D44
V2 = 2
RESET, STEP, STEP_ACK, RESET_ACK, MOTOR_STATE = 1, 2, 3, 5, 8
SECTION_MARKER = 0x5343


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("socket closed")
        data.extend(chunk)
    return bytes(data)


def read_frame(sock):
    body_len = struct.unpack("<I", exact(sock, 4))[0]
    return exact(sock, body_len)


def frame(kind, step_id, sim_time, payload=b"", dt=0, session=0):
    body = struct.pack("<IHHIQQQI", MAGIC, V2, kind, len(payload),
                       step_id, sim_time, dt, session) + payload
    return struct.pack("<I", len(body)) + body


def read_kind(sock, wanted):
    while True:
        body = read_frame(sock)
        if len(body) >= 40 and struct.unpack_from("<H", body, 6)[0] == wanted:
            return body


def ack_status(body):
    return struct.unpack_from("<I", body, 40)[0]


def motor_payload(command=2.5, section_flags=0, command_flags=9,
                  command_length=None, count=1):
    command_body = struct.pack("<HH", count, command_flags)
    for index in range(count):
        command_body += struct.pack("<Hf", index, command + index)
    if command_length is None:
        command_length = len(command_body)
    return (struct.pack("<HH", 1, 0) +
            struct.pack("<HHI", 2, section_flags, command_length) +
            command_body[:command_length])


def motor_payload_with_marker(command=2.5, section_flags=0, command_flags=9,
                              command_length=None, count=1):
    payload = motor_payload(command=command, section_flags=section_flags,
                            command_flags=command_flags,
                            command_length=command_length, count=count)
    return struct.pack("<HH", 1, SECTION_MARKER) + payload[4:]


def motor_plus_voltage_payload():
    payload = bytearray(motor_payload_with_marker(count=4))
    struct.pack_into("<H", payload, 0, 2)
    voltage = struct.pack("<HHII", 19, 0, 1250000, 0)
    payload.extend(struct.pack("<HHI", 4, 0, len(voltage)))
    payload.extend(voltage)
    return bytes(payload)


sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(2.0)
sock.connect(path)
session = 0x10203040
sock.sendall(frame(RESET, 0, 0, session=session))
reset_ack = read_kind(sock, RESET_ACK)
if ack_status(reset_ack) != 0:
    raise RuntimeError("motor loopback RESET_ACK failed")
qemu_time = struct.unpack_from("<Q", reset_ack, 48)[0]

sim_time = qemu_time + 1
payload = motor_payload_with_marker()
sock.sendall(frame(STEP, 1, sim_time, payload, dt=1, session=session))
ack = read_kind(sock, STEP_ACK)
state = read_kind(sock, MOTOR_STATE)
if ack_status(ack) != 0:
    raise RuntimeError("valid MotorCommand was not accepted")
count = struct.unpack_from("<H", state, 40)[0]
index, position, velocity, effort = struct.unpack_from("<Hfff", state, 42)
if (count, index) != (1, 0) or not math.isclose(position, 1.0):
    raise RuntimeError("unexpected loopback MotorState: %r" %
                       ((count, index, position, velocity, effort),))
if not (math.isclose(velocity, 2.5) and math.isclose(effort, 2.5)):
    raise RuntimeError("MotorCommand was not reflected in MotorState")

# Replaying the exact STEP must return the cached state without invoking a
# second endpoint transaction.
sock.sendall(frame(STEP, 1, sim_time, payload, dt=1, session=session))
if ack_status(read_kind(sock, STEP_ACK)) != 0:
    raise RuntimeError("duplicate STEP_ACK status mismatch")
retry_state = read_kind(sock, MOTOR_STATE)
if struct.unpack_from("<H", retry_state, 40)[0] != 1:
    raise RuntimeError("cached MotorState was not replayed")

# Malformed motor payloads must not advance the step validator.
sock.sendall(frame(STEP, 2, sim_time + 1,
                   motor_payload_with_marker(command_length=4), dt=1,
                   session=session))
sock.settimeout(0.05)
try:
    read_kind(sock, STEP_ACK)
except socket.timeout:
    pass
else:
    raise RuntimeError("malformed MotorCommand was acknowledged")
sock.settimeout(2.0)
sock.sendall(frame(STEP, 2, sim_time + 1,
                   motor_payload(), dt=1, session=session))
if ack_status(read_kind(sock, STEP_ACK)) != 0:
    raise RuntimeError("step validator advanced after malformed command")
read_kind(sock, MOTOR_STATE)

# Section flags are reserved and must be rejected consistently with Python.
sock.sendall(frame(STEP, 3, sim_time + 2,
                   motor_payload_with_marker(section_flags=1), dt=1,
                   session=session))
sock.settimeout(0.05)
try:
    read_kind(sock, STEP_ACK)
except socket.timeout:
    pass
else:
    raise RuntimeError("non-zero section flags were acknowledged")
sock.settimeout(2.0)

# A valid motor + ADC section payload is exactly 60 bytes.  The explicit
# section marker keeps it distinct from the compact 60-byte IMU form.
wide_payload = motor_plus_voltage_payload()
if struct.unpack_from("<H", wide_payload, 0)[0] != 2:
    raise RuntimeError("test payload did not contain two sections")
if len(wide_payload) != 60:
    raise RuntimeError("test payload did not hit the 60-byte section boundary")
sock.sendall(frame(STEP, 4, sim_time + 3, wide_payload, dt=2,
                   session=session))
if ack_status(read_kind(sock, STEP_ACK)) != 0:
    raise RuntimeError("60-byte section STEP was not accepted")
wide_state = read_kind(sock, MOTOR_STATE)
if struct.unpack_from("<H", wide_state, 40)[0] != 4:
    raise RuntimeError("60-byte section motor state was not returned")
print("RESULT: QEMU v2 motor callback/validation smoke passed")
PY
