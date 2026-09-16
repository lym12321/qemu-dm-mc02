#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

blocked=0
for command_name in uv; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf 'blocked: %s is required\n' "$command_name" >&2
        blocked=1
    fi
done
if [[ ! -x "$qemu_bin" ]]; then
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    blocked=1
fi
if [[ ! -f "$root_dir/tools/dm_mc02_sim_worker.py" ]]; then
    printf 'blocked: worker not found: %s\n' \
        "$root_dir/tools/dm_mc02_sim_worker.py" >&2
    blocked=1
fi
if [[ ! -f "$root_dir/tools/run-mc02-smoke.sh" ]]; then
    printf 'blocked: QEMU smoke script not found\n' >&2
    blocked=1
fi
if ! command -v timeout >/dev/null 2>&1; then
    printf 'blocked: timeout is required by the QEMU smoke\n' >&2
    blocked=1
fi
((blocked == 0)) || exit 1

# The worker has no external physics or wall-clock input in this run.  The
# monotonic clock below measures host cost only; virtual time stays fixed by
# the worker's deterministic rate/sequence path.
exec uv run --project "$root_dir" python - "$root_dir" "$qemu_bin" \
    "$root_dir/tools/run-worker.sh" <<'PY'
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

root, qemu, worker_launcher = sys.argv[1:]
qemu_smoke = os.path.join(root, "tools", "run-mc02-smoke.sh")
frames = 10_000
magic, version, header = 0x32434D44, 1, 28
reset, imu, telemetry = 1, 2, 3

def frame(kind, sequence, virtual_time_ns, payload=b""):
    body = struct.pack("<IHHIQQ", magic, version, kind, len(payload),
                       sequence, virtual_time_ns) + payload
    return struct.pack("<I", len(body)) + body

def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise RuntimeError("worker closed the socket")
        data.extend(part)
    return bytes(data)

def recv_frame(sock):
    length = struct.unpack("<I", recv_exact(sock, 4))[0]
    if length < header or length > header + 128:
        raise RuntimeError("invalid worker frame length: %d" % length)
    body = recv_exact(sock, length)
    fields = struct.unpack_from("<IHHIQQ", body)
    if fields[0:2] != (magic, version) or fields[3] != length - header:
        raise RuntimeError("invalid worker frame header: %r" % (fields,))
    return fields[2], fields[4], fields[5], body[header:]

with tempfile.TemporaryDirectory(prefix="dm-mc02-perf-") as temp_dir:
    socket_path = os.path.join(temp_dir, "cosim.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)
    server.settimeout(3.0)
    command = [worker_launcher, "--cosim", socket_path,
               "--rate", "1000", "--frames", str(frames),
               "--motor-count", "4"]
    worker_start = time.monotonic_ns()
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        client, _ = server.accept()
        client.settimeout(3.0)
        # This fixed telemetry establishes virtual time zero without waiting
        # on host scheduling. It is not used as a timing source for stepping.
        client.sendall(frame(telemetry, 0, 0, struct.pack("<III", 0, 0, 0)))
        kind, sequence, virtual_time, payload = recv_frame(client)
        worker_ready = time.monotonic_ns()
        if (kind, sequence, virtual_time, payload) != (reset, 0, 0, b""):
            raise RuntimeError("worker RESET mismatch")
        sample_start = time.monotonic_ns()
        previous_time = 0
        expected_payload = struct.pack("<6f", 0.0, 0.0, 0.0,
                                        0.0, 0.0, 1.0)
        for expected_sequence in range(1, frames + 1):
            kind, sequence, virtual_time, payload = recv_frame(client)
            if (kind, sequence, virtual_time) != (imu, expected_sequence,
                                                   expected_sequence * 1_000_000):
                raise RuntimeError("IMU ordering mismatch at %d" %
                                   expected_sequence)
            if payload != expected_payload:
                raise RuntimeError("NullEngine payload mismatch")
        sample_end = time.monotonic_ns()
        client.close()
        stdout, stderr = process.communicate(timeout=3.0)
        if process.returncode != 0:
            raise RuntimeError("worker failed: %s" % stderr.strip())
    except Exception:
        process.kill()
        process.communicate(timeout=3.0)
        raise
    finally:
        server.close()

worker_startup_ms = (worker_ready - worker_start) / 1_000_000.0
throughput_seconds = (sample_end - sample_start) / 1_000_000_000.0
throughput = frames / throughput_seconds if throughput_seconds else 0.0
print("RESULT: NullEngine performance baseline passed")
print("  frames: %d" % frames)
print("  worker startup to RESET: %.3f ms" % worker_startup_ms)
print("  worker sample interval: %.6f s" % throughput_seconds)
print("  NullEngine throughput: %.0f IMU frames/s" % throughput)

qemu_start = time.monotonic_ns()
qemu_result = subprocess.run(
    ["bash", qemu_smoke], env={**os.environ, "QEMU_SYSTEM_ARM": qemu},
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10.0,
)
qemu_end = time.monotonic_ns()
if qemu_result.returncode != 0:
    output = (qemu_result.stdout + qemu_result.stderr).strip()
    raise RuntimeError("QEMU dm-mc02 smoke failed:\n%s" % output)
qemu_wall_ms = (qemu_end - qemu_start) / 1_000_000.0
print("RESULT: dm-mc02 startup smoke passed")
print("  smoke wall-clock: %.3f ms" % qemu_wall_ms)
PY
