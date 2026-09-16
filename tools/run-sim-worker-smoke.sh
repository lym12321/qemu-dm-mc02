#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
command -v uv >/dev/null 2>&1 || { printf '%s\n' 'blocked: uv is required' >&2; exit 1; }

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-worker.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
worker_pid=''
cleanup() {
    if [[ -n "$worker_pid" ]] && kill -0 "$worker_pid" 2>/dev/null; then
        kill "$worker_pid" 2>/dev/null || true
        wait "$worker_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

uv run --project "$root_dir" python - "$cosim_socket" \
    "$root_dir/tools/run-worker.sh" <<'PY'
import os
import socket
import struct
import subprocess
import sys

path, worker_launcher = sys.argv[1:]
MAGIC, VERSION, HEADER = 0x32434D44, 1, 28
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
process = subprocess.Popen([
    worker_launcher, "--cosim", path, "--rate", "333", "--frames", "5"
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
client, _ = server.accept()

def exact(size):
    result = bytearray()
    while len(result) < size:
        part = client.recv(size - len(result))
        if not part:
            raise RuntimeError("worker closed the co-sim socket")
        result.extend(part)
    return bytes(result)

def frame():
    length = struct.unpack("<I", exact(4))[0]
    body = exact(length)
    magic, version, kind, payload_len, sequence, virtual_time_ns = \
        struct.unpack_from("<IHHIQQ", body)
    if (magic, version, payload_len) != (MAGIC, VERSION, length - HEADER):
        raise RuntimeError("worker emitted an invalid frame")
    return kind, sequence, virtual_time_ns, body[HEADER:]

if frame()[:3] != (1, 0, 0):
    raise RuntimeError("worker did not establish RESET session")
previous_time = 0
for expected_sequence in range(1, 6):
    kind, sequence, virtual_time_ns, payload = frame()
    expected_time = round(expected_sequence * 1_000_000_000 / 333)
    if (kind != 2 or sequence != expected_sequence or
            virtual_time_ns != expected_time or virtual_time_ns <= previous_time):
        raise RuntimeError("worker IMU ordering mismatch")
    if struct.unpack("<6f", payload) != (0.0, 0.0, 0.0, 0.0, 0.0, 1.0):
        raise RuntimeError("NullEngine IMU payload mismatch")
    previous_time = virtual_time_ns
client.close()
server.close()
stdout, stderr = process.communicate(timeout=2)
if process.returncode != 0:
    raise RuntimeError("worker failed: %s" % stderr)
print("RESULT: external simulation worker protocol smoke passed")
PY
