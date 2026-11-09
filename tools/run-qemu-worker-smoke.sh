#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
worker_entry="$script_dir/run-worker.sh"
fdcan_arg=""
chassis_harness=()
chassis_qemu=()
if [[ $# -ne 0 ]]; then
    [[ $# -eq 1 && $1 == --chassis ]] || {
        printf 'usage: %s [--chassis]\n' "$0" >&2
        exit 2
    }
    worker_entry="$script_dir/run-chassis-worker.sh"
    fdcan_arg="{fdcan}"
    chassis_harness=(--socket fdcan)
    chassis_qemu=(-chardev "socket,id=fdcan,path={fdcan},server=on,wait=off"
                  -serial null -serial null -serial null -serial null -serial null -serial null
                  -serial chardev:fdcan)
fi
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-qemu-worker.XXXXXX")
guest_elf="$run_dir/readback.elf"
trap 'rm -rf -- "$run_dir"' EXIT
oracle=""
if [[ -n "$fdcan_arg" ]]; then
    oracle="$run_dir/mujoco-imu-oracle"
    native_flags=()
    if [[ -n "${MUJOCO_DYNAMIC_LINK_DIR:-}" ]]; then
        native_flags+=("-L$MUJOCO_DYNAMIC_LINK_DIR" "-Wl,-rpath,$MUJOCO_DYNAMIC_LINK_DIR")
        native_include=${MUJOCO_INCLUDE_DIR:-"$MUJOCO_DYNAMIC_LINK_DIR/../include"}
        native_flags+=("-I$native_include")
    fi
    "${CC:-cc}" "$root_dir/smoke/support/mujoco_imu_oracle.c" \
        "${native_flags[@]}" -lmujoco -o "$oracle"
fi

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.c"

python3 - "$guest_elf" "$qemu_bin" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys
elf, qemu = (Path(value).resolve() for value in sys.argv[1:])
print("WORKER_NATIVE_INPUTS: " + json.dumps({
    "elf_sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
    "qemu_path": str(qemu),
    "qemu_sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
    "qemu_version": subprocess.run([str(qemu), "--version"], check=True,
        capture_output=True, text=True, timeout=2).stdout.splitlines()[0],
    "python": sys.version,
}, sort_keys=True))
PY

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim --socket qmp \
    "${chassis_harness[@]}" \
    --python-arg "{cosim}" --python-arg "{qmp}" --python-arg "$worker_entry" \
    --python-arg "$fdcan_arg" --python-arg "$oracle" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim "${chassis_qemu[@]}" -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
from dm_mc02_test_harness import stop
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

cosim, qmp, worker, fdcan, oracle = sys.argv[1:]
control = QmpSession(qmp, timeout=2)

command = control.command

command("cont")

# The readback guest programs BMI088 ODR before it accepts physical samples.
# Start the worker only after that marker, otherwise a fast host can deliver
# all finite samples during sensor configuration and make this integration
# test pass without exercising the guest-visible data path.
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    memory = command("human-monitor-command", {
        "command-line": "xp /8wx 0x20000000"
    })
    guest_words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", memory or "")]
    if len(guest_words) >= 8 and guest_words[7] == 0x434f4e46:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not finish BMI088 configuration")

worker_command = [worker]
if fdcan:
    worker_command += ["--fdcan", fdcan]
process = subprocess.Popen(worker_command + [
    "--cosim", cosim, "--protocol", "v2",
    "--wait-step-done", "--rate", "1000", "--frames", "1"
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

primary = None
try:
    stdout, stderr = process.communicate(timeout=5)
except BaseException as error:
    primary = error
    raise
finally:
    try:
        stop(process, process_group=False)
    except BaseException as error:
        if primary is None:
            raise
        primary.add_note(f"worker cleanup failed: {error}")
if process.returncode != 0:
    raise RuntimeError("worker failed: %s" % stderr)

deadline = time.monotonic() + 2.0
guest_words = []
while time.monotonic() < deadline:
    memory = command("human-monitor-command", {
        "command-line": "xp /13wx 0x20000000"
    })
    guest_words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", memory or "")]
    # RBK1 + CONF prove that the guest reached its sensor transaction, while
    # a non-zero sensor time proves at least one worker IMU sample was
    # consumed by the BMI088 model rather than merely sent to a socket.
    if (len(guest_words) >= 13 and guest_words[0] == 0x52424b31 and
            guest_words[7] == 0x434f4e46 and guest_words[12] != 0):
        break
    time.sleep(0.01)
if not (len(guest_words) >= 13 and guest_words[0] == 0x52424b31 and
        guest_words[7] == 0x434f4e46 and guest_words[12] != 0):
    raise RuntimeError("worker IMU was not consumed by guest: %r" %
                       guest_words)
if fdcan:
    # Independent native observation, with no production profile/getter/unit
    # helper. The initial contact transient is not assumed to be static 1g.
    native = json.loads(subprocess.check_output([
        oracle, str(Path(worker).resolve().parents[1] / "models/four_wheel.xml")],
        timeout=2))
    native_time, gyro, accel = native[0], native[1:4], native[4:7]
    # Check the actual float32 wire units against Bosch Rev1.9 p39/p27 half-LSB
    # bins (+/-1000dps and +/-6g); sensortime tick is 39.0625us on p27.
    import struct
    units = tuple(math.degrees(float(v)) for v in gyro) + tuple(
        float(v) / 9.80665 for v in accel)
    units = struct.unpack("<6f", struct.pack("<6f", *units))
    raw = [v if v < 32768 else v - 65536 for v in guest_words[1:7]]
    for expected, code, scale in zip(units, raw, [1000] * 3 + [6] * 3):
        if (not math.isfinite(expected) or abs(expected) >= scale
                or abs(expected - code * scale / 32768) > scale / 65536):
            raise RuntimeError("shared chassis target misses official half-LSB bin")
    if guest_words[10:13] != [0, 0, 25]:
        raise RuntimeError("shared chassis first target raw/time mismatch: %r" % guest_words)
    print("CHASSIS_WORKER_EVIDENCE: " + json.dumps({
        "dt_ns": 1_000_000, "sensor_ticks": 25, "native_time_s": native_time,
        "gyro_dps_float32": units[:3], "accel_g_float32": units[3:],
        "gyro_raw": raw[:3], "accel_raw": raw[3:], "DONE": "3/0"}))
status = command("query-status")
if not isinstance(status, dict) or status.get("status") != "running":
    raise RuntimeError("QEMU did not remain running: %r" % status)
command("quit")
control.close()
if not fdcan:
    print("RESULT: QEMU-native external worker integration smoke passed")
PY

if [[ -n "$fdcan_arg" ]]; then
    printf '%s\n' 'RESULT: shared chassis worker native IMU consumer and cleanup passed'
fi
