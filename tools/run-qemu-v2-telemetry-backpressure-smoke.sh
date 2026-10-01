#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v uv >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: uv is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-v2-telemetry-backpressure.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
qtest_socket="$run_dir/qtest.sock"
qmp_socket="$run_dir/qmp.sock"
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
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$cosim_socket" && -S "$qtest_socket" && -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$cosim_socket" && -S "$qtest_socket" && -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

timeout 20s uv run --project "$root_dir" python - \
    "$cosim_socket" "$qtest_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import socket
import struct
import sys
import threading
import time

COSIM_SOCKET, QTEST_SOCKET, QMP_SOCKET = sys.argv[1:]
MAGIC = 0x32434D44
RESET, DIAGNOSTICS = 1, 4
LEGACY_TELEMETRY, V2_TELEMETRY = 3, 7
REQUEST_COUNT = 20_000

def exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("socket closed")
        result.extend(chunk)
    return bytes(result)

def read_body(sock):
    body_len = struct.unpack("<I", exact(sock, 4))[0]
    return exact(sock, body_len)

def read_v2_kind(sock, wanted_kind):
    while True:
        body = read_body(sock)
        if (struct.unpack_from("<H", body, 4)[0] == 2 and
                struct.unpack_from("<H", body, 6)[0] == wanted_kind):
            return body

def v2_frame(kind, step_id, timestamp_ns):
    body = struct.pack("<IHHIQQQI", MAGIC, 2, kind, 0,
                       step_id, timestamp_ns, 0, 0)
    return struct.pack("<I", len(body)) + body

def read_qtest_ok(sock):
    response = bytearray()
    while not response.endswith(b"\n"):
        chunk = sock.recv(64)
        if not chunk:
            raise RuntimeError("qtest socket closed")
        response.extend(chunk)
    if response != b"OK\n":
        raise RuntimeError("qtest writel failed: %r" % bytes(response))

def read_qtest_line(sock):
    response = bytearray()
    while not response.endswith(b"\n"):
        chunk = sock.recv(64)
        if not chunk:
            raise RuntimeError("qtest socket closed")
        response.extend(chunk)
    return bytes(response)

import re

# qtest wait=on keeps QEMU initialization behind this connection.  This is
# required because the qtest server's lifetime timer is created on OPENED.
qtest = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
qtest.settimeout(3.0)
for _ in range(100):
    try:
        qtest.connect(QTEST_SOCKET)
        break
    except ConnectionRefusedError:
        time.sleep(0.01)
else:
    raise RuntimeError("could not connect to qtest socket")

qmp = QmpSession(QMP_SOCKET, timeout=3.0)

qmp_command = qmp.command

cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
cosim.settimeout(10.0)
cosim.connect(COSIM_SOCKET)
read_body(cosim)  # initial v1 compatibility telemetry
cosim.sendall(v2_frame(RESET, 0, 0))
reset_ack = read_v2_kind(cosim, 5)
if struct.unpack_from("<H", reset_ack, 6)[0] != 5:
    raise RuntimeError("missing v2 RESET_ACK")

send_error = []

def send_requests():
    try:
        for index in range(1, REQUEST_COUNT + 1):
            cosim.sendall(v2_frame(DIAGNOSTICS, index, index))
    except BaseException as exc:
        send_error.append(exc)

sender = threading.Thread(target=send_requests, daemon=True)
sender.start()
time.sleep(0.1)
if not sender.is_alive():
    raise RuntimeError("test did not establish control-plane backpressure")

for _ in range(100):
    diagnostics_text = qmp_command("qom-get", {
        "path": "/machine", "property": "cosim-diagnostics"})
    match = re.search(r"(?:^|,)tx_queue=(\d+)(?:,|$)", diagnostics_text)
    if match and int(match.group(1)) == 8:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("control TX queue did not become full: %r" %
                       diagnostics_text)

# PC13 is the board power-output enable reflected in telemetry board_flags bit 0.
# The write occurs while all eight control TX slots are occupied.
qtest.sendall(b"writel 0x58020814 0x2000\n")
read_qtest_ok(qtest)
if qmp_command("qom-get", {"path": "/machine",
                              "property": "out2-enabled"}) is not True:
    raise RuntimeError("qtest did not set PC13/out2-enabled")
pending_text = qmp_command("qom-get", {"path": "/machine",
                                       "property": "cosim-diagnostics"})
if not re.search(r"(?:^|,)telemetry_pending=1(?:,|$)", pending_text):
    raise RuntimeError("PC13 change did not enter deferred telemetry state: %r" %
                       pending_text)
# Keep the qtest connection open until the shell cleanup terminates QEMU.  The
# qtest server in this pinned QEMU can receive a second CLOSED event if the
# client closes immediately after a command.

clock_error = []
clock_stop = threading.Event()

def advance_qtest_clock():
    try:
        while not clock_stop.is_set():
            qtest.sendall(b"clock_step 1000000\n")
            response = read_qtest_line(qtest)
            if not response.startswith(b"OK "):
                raise RuntimeError("qtest clock_step failed: %r" % response)
    except BaseException as exc:
        clock_error.append(exc)

clock_thread = threading.Thread(target=advance_qtest_clock, daemon=True)
clock_thread.start()

diagnostics = 0
telemetry_seen = False
telemetry_after_diagnostics = 0
while diagnostics < REQUEST_COUNT:
    body = read_body(cosim)
    version = struct.unpack_from("<H", body, 4)[0]
    kind = struct.unpack_from("<H", body, 6)[0]
    if version == 2 and kind == DIAGNOSTICS:
        diagnostics += 1
        step_id = struct.unpack_from("<Q", body, 12)[0]
        if step_id != diagnostics:
            raise RuntimeError("diagnostics response gap: %d != %d" %
                               (step_id, diagnostics))
    elif version == 1 and kind == LEGACY_TELEMETRY:
        board_flags = body[28 + 9]
        telemetry_after_diagnostics = diagnostics
        if board_flags & 1:
            telemetry_seen = True
    elif version == 2 and kind == V2_TELEMETRY:
        board_flags = struct.unpack_from("<I", body, 40 + 12)[0]
        telemetry_after_diagnostics = diagnostics
        if board_flags & 1:
            telemetry_seen = True

if not telemetry_seen:
    print("deferred telemetry diagnostics:",
          qmp_command("qom-get", {"path": "/machine",
                                    "property": "cosim-diagnostics"}),
          flush=True)
    # The final control response can drain before the deferred telemetry is
    # flushed. Keep virtual time moving while the remaining TX work settles.
    cosim.settimeout(0.2)
    deadline = time.monotonic() + 3.0
    while not telemetry_seen and time.monotonic() < deadline:
        try:
            body = read_body(cosim)
        except TimeoutError:
            continue
        version = struct.unpack_from("<H", body, 4)[0]
        kind = struct.unpack_from("<H", body, 6)[0]
        if version == 1 and kind == LEGACY_TELEMETRY:
            telemetry_after_diagnostics = diagnostics
            telemetry_seen = bool(body[28 + 9] & 1)
        elif version == 2 and kind == V2_TELEMETRY:
            telemetry_after_diagnostics = diagnostics
            telemetry_seen = bool(
                struct.unpack_from("<I", body, 40 + 12)[0] & 1)

clock_stop.set()
clock_thread.join(timeout=2.0)
if clock_thread.is_alive():
    raise RuntimeError("qtest clock thread remained blocked")
if clock_error:
    raise RuntimeError("qtest clock failed: %s" % clock_error[0])
sender.join(timeout=2.0)
if sender.is_alive():
    raise RuntimeError("request sender remained blocked after response drain")
if send_error:
    raise RuntimeError("request sender failed: %s" % send_error[0])
if not telemetry_seen:
    raise RuntimeError("pending telemetry was not retried after control drain")
if telemetry_after_diagnostics < 8:
    raise RuntimeError("telemetry was emitted before the control queue drained")
qmp.close()
qmp.close()
cosim.close()
print("RESULT: QEMU telemetry retry survived full control TX backpressure")
PY
