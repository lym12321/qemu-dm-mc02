#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'SKIP: ROS2 Python environment is unavailable'
    exit 77
}

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-ros2.XXXXXX")
cosim_socket="$run_dir/cosim.sock"
cleanup() { rm -rf -- "$run_dir"; }
trap cleanup EXIT

python3 - "$cosim_socket" "$root_dir/tools/run-worker.sh" <<'PY'
import json
import math
import socket
import struct
import subprocess
import sys

path, worker_launcher = sys.argv[1:]
MAGIC, VERSION, HEADER = 0x32434D44, 1, 28
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
try:
    import rclpy  # noqa: F401
    from rclpy.node import Node  # noqa: F401
    from sensor_msgs.msg import Imu  # noqa: F401
    from sensor_msgs.msg import JointState  # noqa: F401
    from std_msgs.msg import Float64MultiArray  # noqa: F401
except ImportError:
    print("SKIP: ROS2 Python environment is unavailable")
    server.close()
    sys.exit(77)
process = subprocess.Popen([
    worker_launcher, "--cosim", path, "--engine", "ros2",
    "--rate", "1000", "--frames", "5"
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
server.settimeout(5.0)
try:
    client, _ = server.accept()
except socket.timeout as exc:
    process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    raise RuntimeError("ROS2 worker did not connect: %s" % stderr) from exc

def exact(size):
    result = bytearray()
    while len(result) < size:
        data = client.recv(size - len(result))
        if not data:
            raise RuntimeError("ROS2 worker closed the socket")
        result.extend(data)
    return bytes(result)

def frame():
    length = struct.unpack("<I", exact(4))[0]
    body = exact(length)
    header = struct.unpack_from("<IHHIQQ", body)
    return header, body[HEADER:]

header, _ = frame()
if (header[2], header[3], header[4], header[5]) != (1, 0, 0, 0):
    raise RuntimeError("ROS2 worker did not send RESET")
for expected in range(1, 6):
    header, payload = frame()
    if header[2] != 2 or header[4] != expected:
        raise RuntimeError("ROS2 worker sequence mismatch")
    if not all(math.isfinite(value) for value in struct.unpack("<6f", payload)):
        raise RuntimeError("ROS2 worker emitted non-finite IMU")
client.close()
server.close()
stdout, stderr = process.communicate(timeout=5)
if process.returncode != 0:
    raise RuntimeError("ROS2 worker failed: %s" % stderr)
print("RESULT: ROS2 simulation worker smoke passed")
PY
