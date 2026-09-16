#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
elf="$root_dir/build/smoke/dm_mc02_bmi088_fifo_smoke.elf"
machine=${DM_MC02_MACHINE:-dm-mc02}

[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }
if [[ ! -x "$elf" || "$root_dir/smoke/dm_mc02_bmi088_fifo_smoke.c" -nt "$elf" ||
      "$root_dir/smoke/dm_mc02_bmi088_fifo_smoke.ld" -nt "$elf" ]]; then
    mkdir -p "$(dirname -- "$elf")"
    arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -nostdlib -ffreestanding \
        -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_bmi088_fifo_smoke.ld" \
        -o "$elf" "$root_dir/smoke/dm_mc02_bmi088_fifo_smoke.c"
fi

run_dir=$(mktemp -d "$root_dir/output.bmi088-fifo-smoke.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
cosim_socket="$run_dir/cosim.sock"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -f -- "$qmp_socket" "$cosim_socket" "$run_dir/qemu.stderr"
    rmdir -- "$run_dir"
}
trap cleanup EXIT

"$qemu_bin" -machine "$machine" -kernel "$elf" -nodefaults -display none \
    -monitor none -S -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 100); do
    [[ -S "$qmp_socket" && -S "$cosim_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" && -S "$cosim_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$qmp_socket" "$cosim_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time

qmp_path, cosim_path = sys.argv[1:]
qmp = QmpSession(qmp_path, timeout=2.0)
qmp_buffer = bytearray()

qmp_command = qmp.command

qmp_command("qom-set", {"path": "/machine", "property": "gpio-input",
                        "value": "A14=0"})
cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.settimeout(2.0)
cosim.connect(cosim_path)

def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise RuntimeError("co-sim chardev closed")
        data.extend(chunk)
    return bytes(data)

# Wait until QEMU has attached the optional serial slot to the binary co-sim
# link; otherwise a very small bare-metal guest can finish before the host
# connection is fully established.
outer = struct.unpack("<I", recv_exact(cosim, 4))[0]
if outer < 40:
    raise RuntimeError(f"invalid initial telemetry length: {outer}")
recv_exact(cosim, outer)
qmp_command("cont")

def words():
    text = qmp_command("human-monitor-command",
                       {"command-line": "xp /60wx 0x20000000"})
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    current = words()
    if len(current) >= 2 and current[1] == 0x4f46464f:
        break
    time.sleep(0.005)
else:
    raise RuntimeError("guest did not reach BMI088 power-off check")

def frame(sequence, timestamp_ns, gyro, accel):
    packet = struct.pack("<IHHIQQ6f", 0x32434D44, 1, 2, 24,
                         sequence, timestamp_ns, *(gyro + accel))
    return struct.pack("<I", len(packet)) + packet

cosim.sendall(frame(1, 1_000_000, (10.0, 0.0, 0.0),
                     (0.10, 0.0, 1.0)))
time.sleep(0.02)
qmp_command("qom-set", {"path": "/machine", "property": "gpio-input",
                        "value": "A14=1"})

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    current = words()
    # The ODR write occurred after FIFO enable and legitimately left one
    # two-byte config frame.  No powered-off sensor data may be present.
    if len(current) >= 4 and current[2:4] == [2, 0]:
        break
    time.sleep(0.005)
else:
    raise RuntimeError(f"powered-off BMI088 accepted data: {current[:4]!r}")

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    current = words()
    if len(current) >= 2 and current[1] == 0x434f4e46:
        break
    time.sleep(0.005)
else:
    raise RuntimeError("guest did not complete BMI088 power-up")

qmp_command("qom-set", {"path": "/machine", "property": "gpio-input",
                        "value": "A14=0"})
for sequence, timestamp, gyro, accel in [
    (2, 2_000_000, (20.0, 0.0, 0.0), (0.20, 0.0, 1.0)),
    (3, 3_000_000, (30.0, 0.0, 0.0), (0.30, 0.0, 1.0)),
    (4, 4_000_000, (40.0, 0.0, 0.0), (0.40, 0.0, 1.0)),
]:
    cosim.sendall(frame(sequence, timestamp, gyro, accel))
time.sleep(0.02)
qmp_command("qom-set", {"path": "/machine", "property": "gpio-input",
                        "value": "A14=1"})

deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    current = words()
    if len(current) >= 60 and current[59] == 0x444f4e45:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not finish FIFO readout")

if current[0] != 0x4649464F:
    raise RuntimeError(f"FIFO marker mismatch: {current[:2]!r}")
if current[2] != 23 or current[3] != 3:
    raise RuntimeError(f"FIFO fill mismatch: accel={current[2]} gyro={current[3]}")
if current[4:7] != [0x48, 1, 0x84]:
    raise RuntimeError(f"accelerometer config/partial header mismatch: {current[4:7]!r}")
if [current[index] for index in (7, 14, 21)] != [0x84, 0x84, 0x84]:
    raise RuntimeError(f"accelerometer FIFO headers invalid: {current[7:32]!r}")
if current[28] != 0x44:
    raise RuntimeError(f"accelerometer sensortime frame missing: {current[28:32]!r}")
sensor_time = current[29] | (current[30] << 8) | (current[31] << 16)
if sensor_time != 102:
    raise RuntimeError(f"accelerometer sensortime frame mismatch: {sensor_time}")
if current[58] != 0x80:
    raise RuntimeError(f"accelerometer FIFO overread mismatch: {current[58]:#x}")

gyro_x = []
for index in (32, 40, 48):
    raw = current[index] | (current[index + 1] << 8)
    gyro_x.append(raw - 0x10000 if raw & 0x8000 else raw)
if not (0 < gyro_x[0] < gyro_x[1] < gyro_x[2]):
    raise RuntimeError(f"gyro FIFO samples are not ordered: {gyro_x!r}")
print("RESULT: BMI088 accel/gyro FIFO smoke passed")
print("  fill bytes/frames:", current[2], current[3], "gyro x:", gyro_x)
qmp_command("quit")
qmp.close()
cosim.close()
PY

exit_status=0
for _ in $(seq 1 200); do
    if ! kill -0 "$qemu_pid" 2>/dev/null; then
        wait "$qemu_pid" || exit_status=$?
        break
    fi
    sleep 0.01
done
if kill -0 "$qemu_pid" 2>/dev/null; then
    printf '%s\n' 'QEMU did not exit cleanly after QMP quit' >&2
    exit 1
fi
exit "$exit_status"
