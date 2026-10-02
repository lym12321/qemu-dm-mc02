#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-qemu-worker.XXXXXX")
guest_elf="$run_dir/readback.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim --socket qmp \
    --python-arg "{cosim}" --python-arg "{qmp}" --python-arg "$root_dir/tools/run-worker.sh" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import subprocess
import sys
import time

cosim, qmp, worker = sys.argv[1:]
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

process = subprocess.Popen([
    worker, "--cosim", cosim, "--protocol", "v2",
    "--wait-step-done", "--rate", "1000", "--frames", "1"
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

stdout, stderr = process.communicate(timeout=5)
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
status = command("query-status")
if not isinstance(status, dict) or status.get("status") != "running":
    raise RuntimeError("QEMU did not remain running: %r" % status)
command("quit")
control.close()
print("RESULT: QEMU-native external worker integration smoke passed")
PY
