#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
guest_elf="$root_dir/build/smoke/dm_mc02_bmi088_smoke.elf"

command -v uv >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: uv is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
[[ -x "$guest_elf" ]] || "$script_dir/build-bmi088-smoke.sh" >/dev/null

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-v2-backpressure.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none \
    -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$cosim_socket" ]] && break
    sleep 0.01
done
[[ -S "$cosim_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

timeout 20s uv run --project "$root_dir" python - "$cosim_socket" <<'PY'
import socket
import struct
import sys
import threading
import time

SOCKET = sys.argv[1]
MAGIC = 0x32434D44
RESET, DIAGNOSTICS, RESET_ACK = 1, 4, 5
V2_HEADER_SIZE = 36
REQUEST_COUNT = 20_000


def exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("co-sim socket closed")
        result.extend(chunk)
    return bytes(result)


def read_body(sock):
    body_len = struct.unpack("<I", exact(sock, 4))[0]
    return exact(sock, body_len)


def read_v2(sock, wanted_kind=None):
    while True:
        body = read_body(sock)
        if (len(body) >= 8 and struct.unpack_from("<H", body, 4)[0] == 2 and
                (wanted_kind is None or
                 struct.unpack_from("<H", body, 6)[0] == wanted_kind)):
            return body


def v2_frame(kind, step_id, timestamp_ns):
    body = struct.pack("<IHHIQQQI", MAGIC, 2, kind, 0,
                       step_id, timestamp_ns, 0, 0)
    return struct.pack("<I", len(body)) + body


sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
sock.settimeout(10.0)
sock.connect(SOCKET)
read_body(sock)  # initial compatibility telemetry
sock.sendall(v2_frame(RESET, 0, 0))
reset_ack = read_v2(sock, RESET_ACK)
if struct.unpack_from("<H", reset_ack, 6)[0] != RESET_ACK:
    raise RuntimeError("missing RESET_ACK before backpressure test")

send_error = []


def send_requests():
    try:
        for index in range(1, REQUEST_COUNT + 1):
            sock.sendall(v2_frame(DIAGNOSTICS, index, index))
    except BaseException as exc:
        send_error.append(exc)


sender = threading.Thread(target=send_requests, daemon=True)
sender.start()
time.sleep(0.1)
if not sender.is_alive():
    raise RuntimeError("test did not establish co-sim socket backpressure")

for expected in range(1, REQUEST_COUNT + 1):
    body = read_v2(sock, DIAGNOSTICS)
    kind = struct.unpack_from("<H", body, 6)[0]
    payload_len = struct.unpack_from("<I", body, 8)[0]
    step_id = struct.unpack_from("<Q", body, 12)[0]
    timestamp_ns = struct.unpack_from("<Q", body, 20)[0]
    if (kind, payload_len, step_id, timestamp_ns) != (
            DIAGNOSTICS, 40, expected, expected):
        raise RuntimeError(
            "v2 control response gap at %d: kind=%d payload=%d step=%d time=%d" %
            (expected, kind, payload_len, step_id, timestamp_ns))

sender.join(timeout=2.0)
if sender.is_alive():
    raise RuntimeError("request sender remained blocked after response drain")
if send_error:
    raise RuntimeError("request sender failed: %s" % send_error[0])
sock.close()
print("RESULT: QEMU v2 control backpressure preserved %d responses" %
      REQUEST_COUNT)
PY
