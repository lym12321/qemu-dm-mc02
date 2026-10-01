#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-v2-adc-only.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
qemu_pid=''
cleanup() {
    local status=$?
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
    return "$status"
}
trap cleanup EXIT

"$qemu_bin" -machine dm-mc02 -nodefaults -display none -monitor none -S \
    -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$cosim_socket" ]] && break
    sleep 0.01
done
[[ -S "$cosim_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$cosim_socket" <<'PY'
import socket
import struct
import sys

socket_path = sys.argv[1]


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


def read_v2(sock, wanted_kind):
    while True:
        body = read_frame(sock)
        if (struct.unpack_from("<H", body, 4)[0] == 2 and
                struct.unpack_from("<H", body, 6)[0] == wanted_kind):
            return body


def frame(kind, step_id, t_sim_ns, payload=b"", dt_ns=0, session_id=0):
    body = struct.pack("<IHHIQQQI", 0x32434D44, 2, kind,
                       len(payload), step_id, t_sim_ns, dt_ns,
                       session_id) + payload
    return struct.pack("<I", len(body)) + body


sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(5.0)
sock.connect(socket_path)
read_frame(sock)  # legacy open snapshot

session_id = 0x13572468
sock.sendall(frame(1, 0, 0, session_id=session_id))
reset_ack = read_v2(sock, 5)
if struct.unpack_from("<I", reset_ack, 40)[0] != 0:
    raise RuntimeError("v2 RESET_ACK was not successful")

# Duplicate ADC_INPUT channels are invalid and must not advance the validator.
duplicate_adc_payload = (struct.pack("<HH", 2, 0x5343) +
                          struct.pack("<HHI", 3, 0, 8) +
                          struct.pack("<HHI", 4, 4660, 0) +
                          struct.pack("<HHI", 3, 0, 8) +
                          struct.pack("<HHI", 4, 22136, 0))
sock.sendall(frame(2, 1, 1, duplicate_adc_payload, dt_ns=1,
                   session_id=session_id))
sock.settimeout(0.05)
try:
    read_v2(sock, 3)
except socket.timeout:
    pass
else:
    raise RuntimeError("duplicate ADC channel was acknowledged")
sock.settimeout(5.0)

# One ADC_INPUT section is a complete valid STEP; no IMU section is needed.
adc_payload = (struct.pack("<HH", 1, 0) +
               struct.pack("<HHI", 3, 0, 8) +
               struct.pack("<HHI", 4, 4660, 0))
sock.sendall(frame(2, 1, 1, adc_payload, dt_ns=1,
                   session_id=session_id))
step_ack = read_v2(sock, 3)
if (struct.unpack_from("<Q", step_ack, 12)[0] != 1 or
        struct.unpack_from("<Q", step_ack, 20)[0] != 1 or
        struct.unpack_from("<I", step_ack, 40)[0] != 0):
    raise RuntimeError("ADC-only v2 STEP was not accepted")
print("RESULT: QEMU v2 ADC-only STEP smoke passed")
PY
