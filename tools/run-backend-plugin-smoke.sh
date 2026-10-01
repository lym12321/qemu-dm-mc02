#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
command -v uv >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: uv is required' >&2
    exit 1
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-backend.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
cleanup() { rm -rf -- "$run_dir"; }
trap cleanup EXIT

uv run --project "$root_dir" python - "$cosim_socket" \
    "$root_dir/tools/run-worker.sh" "$root_dir/smoke" <<'PY'
import os
import socket
import struct
import subprocess
import sys

path, worker, plugin_path = sys.argv[1:]
magic, version, header = 0x32434D44, 1, 28
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
environment = os.environ.copy()
environment["PYTHONPATH"] = plugin_path + os.pathsep + environment.get(
    "PYTHONPATH", "")
process = subprocess.Popen([
    worker, "--cosim", path, "--backend",
    "dm_mc02_backend_fixture:create_backend", "--frames", "5",
], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise RuntimeError("custom backend worker closed the socket")
        data.extend(part)
    return bytes(data)

def frame(sock):
    length = struct.unpack("<I", exact(sock, 4))[0]
    body = exact(sock, length)
    fields = struct.unpack_from("<IHHIQQ", body)
    if fields[:2] != (magic, version) or fields[3] != length - header:
        raise RuntimeError("custom backend emitted an invalid frame")
    return fields[2], fields[4], fields[5], body[header:]

try:
    client, _ = server.accept()
    kind, sequence, timestamp, payload = frame(client)
    if (kind, sequence, timestamp, payload) != (1, 0, 0, b""):
        raise RuntimeError("custom backend RESET mismatch")
    expected = struct.pack("<6f", 1.0, 2.0, 3.0, 0.0, 0.0, 1.0)
    for sequence in range(1, 6):
        kind, received_sequence, timestamp, payload = frame(client)
        if (kind, received_sequence) != (2, sequence) or payload != expected:
            raise RuntimeError("custom backend IMU mismatch")
    client.close()
    stdout, stderr = process.communicate(timeout=3)
    if process.returncode != 0:
        raise RuntimeError("custom backend worker failed: %s" % stderr)
finally:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=2)
    server.close()

print("RESULT: external backend plugin smoke passed")
PY
