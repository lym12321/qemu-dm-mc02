#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'RESULT: blocked (arm-none-eabi-gcc is required)' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'RESULT: blocked (python3 is required)' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'RESULT: blocked (QEMU not found: %s)\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-calibration-data.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
cosim_socket="$run_dir/cosim.sock"
guest_elf="$run_dir/adc-calibration-data.elf"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_adc_calibration_data_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_calibration_data_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 300); do
    [[ -S "$cosim_socket" && -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$cosim_socket" && -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$cosim_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time

COSIM, QMP = sys.argv[1:]
MAGIC = 0x32434D44

def frame(kind, sequence, timestamp, payload=b""):
    body = struct.pack("<IHHIQQ", MAGIC, 1, kind, len(payload), sequence, timestamp) + payload
    return struct.pack("<I", len(body)) + body

def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise RuntimeError("socket closed")
        data.extend(chunk)
    return bytes(data)

def words(sock, address, count):
    text = sock.command("human-monitor-command", {
        "command-line": "xp /%dwx 0x%x" % (count, address)}) or ""
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]

cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.settimeout(2.0)
cosim.connect(COSIM)
outer = struct.unpack("<I", exact(cosim, 4))[0]
exact(cosim, outer)
cosim.sendall(frame(1, 0, 0))
cosim.sendall(frame(5, 1, 1_000_000, struct.pack("<HHI", 4, 0x1234, 0)))
cosim.sendall(frame(5, 2, 2_000_000, struct.pack("<HHI", 19, 0x2a35, 0)))

qmp = QmpSession(QMP, timeout=2.0)
qmp.command("cont")

deadline = time.monotonic() + 2.0
result = []
while time.monotonic() < deadline:
    result = words(qmp, 0x20000000, 22)
    if len(result) >= 19 and result[18] == 0x444f4e45:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("ADC calibration-data result was not observed: %r" % result)

if result[0] != 0x41434431:
    raise RuntimeError("marker mismatch: %r" % result)
if result[2:4] != [0x1234, 0x2a35]:
    raise RuntimeError("raw regular/injected mismatch: %r" % result[2:4])
if result[10] != 0x07ff07ff or result[11] != 0x3fffffff:
    raise RuntimeError("calibration factor saturation mismatch: %r" % result[10:12])
if result[12:14] != [0, 0]:
    raise RuntimeError("calibration factor clear mismatch: %r" % result[12:14])
if result[14:16] != [0, 0]:
    raise RuntimeError("disabled calibration writes were not rejected: %r" % result[14:16])
if result[16] != (4 << 6) or result[17] != (19 << 9):
    raise RuntimeError("regular/injected rank configuration mismatch: %r" % result[16:18])

vectors = result[4:10]
expected = [
    0x1222, 0x2a23,             # offset only
    0x1287, 0x2af5,             # linearity only
    0x1275, 0x2ae3,             # offset followed by linearity
]
if vectors != expected:
    raise RuntimeError("calibration transform mismatch: got %r expected %r" %
                       (vectors, expected))

print("RESULT: ADC calibration-data smoke passed")
print("CALIBRATION: regular/injected offset and linearity vectors passed")
qmp.command("system_reset")
reset_deadline = time.monotonic() + 1.0
reset = []
while time.monotonic() < reset_deadline:
    reset = words(qmp, 0x20000000, 22)
    if len(reset) >= 22 and reset[21] == 0x52535432:
        break
    time.sleep(0.001)
if reset[19:21] != [0, 0]:
    raise RuntimeError("ADC calibration factors survived system_reset: %r" % reset[19:21])
print("RESET: calibration factors cleared")
qmp.command("quit")
qmp.close(); cosim.close()
PY
