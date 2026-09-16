#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
command -v uv >/dev/null 2>&1 || {
    printf '%s\n' 'SKIP: uv is required for the MuJoCo backend'
    exit 77
}
mujoco_python="$root_dir/.venv/bin/python"
if [[ ! -x "$mujoco_python" ]] ||
   ! "$mujoco_python" -c 'import mujoco' >/dev/null 2>&1; then
    printf '%s\n' 'SKIP: MuJoCo Python environment is unavailable'
    exit 77
fi

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-mujoco.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
cleanup() { rm -rf -- "$run_dir"; }
trap cleanup EXIT

UV_NO_SYNC=1 uv run --project "$root_dir" --extra mujoco --frozen --no-sync \
    python - "$cosim_socket" \
    "$root_dir/tools/run-worker.sh" \
    "$root_dir/smoke/dm_mc02_mujoco_smoke.xml" <<'PY'
import json
import math
import os
import socket
import struct
import subprocess
import sys

path, worker_launcher, model = sys.argv[1:]
MAGIC, VERSION, HEADER = 0x32434D44, 1, 28
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
process = subprocess.Popen([
    worker_launcher, "--cosim", path, "--engine", "mujoco",
    "--mujoco-model", model, "--rate", "1000", "--frames", "5"
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
client, _ = server.accept()

def exact(size):
    data = bytearray()
    while len(data) < size:
        part = client.recv(size - len(data))
        if not part:
            raise RuntimeError("MuJoCo worker closed the socket")
        data.extend(part)
    return bytes(data)

def frame():
    length = struct.unpack("<I", exact(4))[0]
    body = exact(length)
    return (struct.unpack_from("<IHHIQQ", body), body[HEADER:])

header, payload = frame()
if (header[2], header[3], header[4], header[5]) != (1, 0, 0, 0):
    raise RuntimeError("MuJoCo worker did not send RESET")
previous_time = 0
for expected in range(1, 6):
    header, payload = frame()
    if header[2] != 2 or header[4] != expected or header[5] <= previous_time:
        raise RuntimeError("MuJoCo worker timestamp ordering mismatch")
    values = struct.unpack("<6f", payload)
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError("MuJoCo worker emitted non-finite IMU")
    previous_time = header[5]
client.close()
server.close()
stdout, stderr = process.communicate(timeout=5)
if process.returncode != 0:
    raise RuntimeError("MuJoCo worker failed: %s" % stderr)
print("RESULT: MuJoCo simulation worker smoke passed")
PY
