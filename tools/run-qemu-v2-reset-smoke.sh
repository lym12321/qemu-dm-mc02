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

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-v2-reset.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/reset.elf"
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
import socket
import struct
import sys
import time

cosim_path, qmp_path = sys.argv[1:]


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    sock.connect(path)
    return sock


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


def v2_frame(kind, step_id, t_sim_ns, payload=b"", dt_ns=0, flags=0):
    body = struct.pack("<IHHIQQQI", 0x32434D44, 2, kind, len(payload),
                       step_id, t_sim_ns, dt_ns, flags) + payload
    return struct.pack("<I", len(body)) + body


def v1_reset_frame():
    body = struct.pack("<IHHIQQ", 0x32434D44, 1, 1, 0, 0, 0)
    return struct.pack("<I", len(body)) + body


def frame_fields(body):
    return (struct.unpack_from("<H", body, 6)[0],
            struct.unpack_from("<Q", body, 20)[0])


def frame_session_id(body):
    return struct.unpack_from("<I", body, 36)[0]


def status(body):
    return struct.unpack_from("<I", body, 40)[0]


def read_v2_frame(sock, wanted_kind=None):
    while True:
        body = read_frame(sock)
        if (struct.unpack_from("<H", body, 4)[0] == 2 and
                (wanted_kind is None or
                 struct.unpack_from("<H", body, 6)[0] == wanted_kind)):
            return body


cosim = connect(cosim_path)
read_frame(cosim)  # legacy open snapshot
initial_session_id = 0x10203040
cosim.sendall(v2_frame(1, 0, 0, flags=initial_session_id))
initial_ack = read_v2_frame(cosim)
if (frame_fields(initial_ack) != (5, 0) or
        frame_session_id(initial_ack) != initial_session_id):
    raise RuntimeError("initial v2 RESET_ACK mismatch")

# A chardev reconnect must preserve the negotiated wire version. The peer
# cannot parse a legacy 28-byte RESET before it has a chance to send v2 RESET.
cosim.close()
time.sleep(0.1)
cosim = connect(cosim_path)
reconnect_reset = read_v2_frame(cosim, 1)
reconnect_ack = read_v2_frame(cosim, 5)
reconnect_reset_kind, reconnect_time = frame_fields(reconnect_reset)
reconnect_ack_kind, reconnect_ack_time = frame_fields(reconnect_ack)
reconnect_session_id = frame_session_id(reconnect_ack)
if (reconnect_reset_kind != 1 or reconnect_ack_kind != 5 or
        reconnect_time == 0 or reconnect_ack_time != reconnect_time or
        reconnect_session_id == 0 or
        reconnect_session_id == initial_session_id):
    raise RuntimeError("v2 reconnect handshake mismatch")
read_v2_frame(cosim, 7)  # forced board telemetry for the new session
cosim.sendall(v2_frame(2, 1, reconnect_time + 1, b"\x00" * 60,
                       dt_ns=1, flags=reconnect_session_id))
reconnect_step_ack = read_v2_frame(cosim, 3)
if (frame_fields(reconnect_step_ack) != (3, reconnect_time + 1) or
        status(reconnect_step_ack) != 0):
    raise RuntimeError("v2 reconnect did not accept the first STEP")
# Replaying the exact STEP is the recovery path for a lost ACK. It must
# produce another ACK without injecting the IMU sample a second time.
cosim.sendall(v2_frame(2, 1, reconnect_time + 1, b"\x00" * 60,
                       dt_ns=1, flags=reconnect_session_id))
duplicate_ack = read_v2_frame(cosim, 3)
if (frame_fields(duplicate_ack) != (3, reconnect_time + 1) or
        status(duplicate_ack) != 0):
    raise RuntimeError("duplicate v2 STEP was not acknowledged")

qmp = QmpSession(qmp_path, timeout=2.0)
time.sleep(0.5)
qmp.command("system_reset")
qmp_diag = qmp.command("qom-get", {
    "path": "/machine", "property": "cosim-diagnostics"})
time.sleep(0.2)

try:
    reset_kind, reset_time = frame_fields(read_v2_frame(cosim, 1))
    reset_body = read_v2_frame(cosim, 5)
except (TimeoutError, socket.timeout) as exc:
    raise RuntimeError(
        "runtime v2 RESET/RESET_ACK not observed; "
        f"qmp_diag={qmp_diag!r}") from exc
ack_kind, ack_time = frame_fields(reset_body)
runtime_session_id = frame_session_id(reset_body)
if (reset_kind != 1 or ack_kind != 5 or reset_time == 0 or
        ack_time != reset_time or runtime_session_id == 0 or
        runtime_session_id == initial_session_id):
    raise RuntimeError(
        "runtime v2 reset session mismatch: "
        f"RESET=({reset_kind}, {reset_time}) "
        f"RESET_ACK=({ack_kind}, {ack_time})")
telemetry = read_v2_frame(cosim, 7)
if struct.unpack_from("<I", telemetry, 8)[0] != 24:
    raise RuntimeError("runtime reset telemetry was not v2 board telemetry")
step_time = reset_time + 1
cosim.sendall(v2_frame(2, 1, step_time, b"\x00" * 60, dt_ns=1,
                       flags=runtime_session_id))
step_kind, step_ack_time = frame_fields(read_v2_frame(cosim))
if step_kind != 3 or step_ack_time != step_time:
    raise RuntimeError(
        "runtime v2 reset did not accept the first STEP: "
        f"STEP_ACK=({step_kind}, {step_ack_time})")
stale_session_id = runtime_session_id ^ 1
if stale_session_id == 0:
    stale_session_id = 0x7fffffff
cosim.sendall(v2_frame(2, 2, step_time + 1, b"\x00" * 60,
                       dt_ns=1, flags=stale_session_id))
cosim.settimeout(0.2)
try:
    read_v2_frame(cosim, 3)
except socket.timeout:
    pass
else:
    raise RuntimeError("stale v2 session frame was accepted")
finally:
    cosim.settimeout(2.0)
cosim.sendall(v2_frame(2, 2, step_time + 1, b"\x00" * 60,
                       dt_ns=1, flags=runtime_session_id))
valid_ack = read_v2_frame(cosim, 3)
if frame_fields(valid_ack) != (3, step_time + 1) or status(valid_ack) != 0:
    raise RuntimeError("valid v2 session frame was not accepted")
imu = b"\x00" * 60
# Non-zero command metadata is legal; the link must preserve it for the
# endpoint instead of rejecting a codec-valid MotorCommand.
motor = struct.pack("<HH", 0, 9)
motor_payload = (struct.pack("<HH", 2, 0) +
                 struct.pack("<HHI", 1, 0, len(imu)) + imu +
                 struct.pack("<HHI", 2, 0, len(motor)) + motor)
cosim.sendall(v2_frame(2, 3, step_time + 2, motor_payload, dt_ns=1,
                       flags=runtime_session_id))
motor_ack = read_v2_frame(cosim, 3)
if frame_fields(motor_ack) != (3, step_time + 2) or status(motor_ack) != 3:
    raise RuntimeError("unsupported v2 motor section did not return status")

# A protocol version is fixed for the lifetime of one chardev session.  A
# legacy RESET must not downgrade the active v2 validator and break the next
# v2 step.
cosim.sendall(v1_reset_frame())
cosim.sendall(v2_frame(2, 4, step_time + 3, b"\x00" * 60,
                       dt_ns=1, flags=runtime_session_id))
continued_ack = read_v2_frame(cosim, 3)
if (frame_fields(continued_ack) != (3, step_time + 3) or
        status(continued_ack) != 0):
    raise RuntimeError("v1 RESET downgraded the active v2 session: "
                       f"fields={frame_fields(continued_ack)} "
                       f"status={status(continued_ack)} "
                       f"session={frame_session_id(continued_ack)}")
print("RESULT: QEMU v2 runtime RESET/RESET_ACK smoke passed")
PY
