#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
[[ -x "$qemu_bin" ]] || qemu_bin="$root_dir/build/qemu/qemu-system-arm"
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-motor.XXXXXX")
guest_elf="$run_dir/motor.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_motor_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_motor_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim --socket can --socket qmp \
    --python-arg "$root_dir" --python-arg "{qmp}" --python-arg "{cosim}" --python-arg "{can}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim \
    -serial none -serial none -serial none -serial none -serial none -serial none \
    -chardev "socket,id=can1,path={can},server=on,wait=off" \
    -serial chardev:can1 -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import os
import re
import subprocess
import sys
import time

root, qmp_path, cosim_path, can_path = sys.argv[1:]
worker = os.path.join(root, "tools", "run-worker.sh")
process = None

qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

def result_words():
    text = command("human-monitor-command", {
        "command-line": "xp /5wx 0x20000000",
    }) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

try:
    command("cont")
    # Deliberately let the guest submit its first CAN frames before the
    # worker connects. This verifies QEMU's pre-open FDCAN TX queue rather
    # than relying on a lucky process scheduling order.
    time.sleep(0.05)
    process = subprocess.Popen([
        worker, "--cosim", cosim_path, "--fdcan", can_path,
        "--engine", "null", "--motor-protocol", "dm-mit", "--rate", "1000",
        "--realtime", "--frames", "5000", "--verbose",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 7.0
    words = []
    while time.monotonic() < deadline:
        words = result_words()
        if words and words[0] == 0x444d4d31:
            if ((words[1] >> 18) & 0x7ff) != 0x11 or ((words[2] >> 16) & 0xf) != 8:
                raise RuntimeError("invalid DM feedback header: %r" % words)
            print("RESULT: DM MIT motor command/feedback closed-loop smoke passed")
            break
        if process is not None and process.poll() is not None and process.returncode != 0:
            break
        time.sleep(0.01)
    else:
        process.terminate()
        stdout, stderr = process.communicate(timeout=3)
        raise RuntimeError("DM motor feedback was not observed: %r; worker stdout=%r stderr=%r" %
                           (words, stdout, stderr))
    if process is not None and process.poll() is None:
        process.terminate()
    stdout, stderr = process.communicate(timeout=3) if process is not None else ("", "")
    # Direct Python termination reports -15; the uv/bash launcher may expose
    # the equivalent shell status 143. Both are expected after feedback was
    # observed and the smoke deliberately stops the still-running worker.
    if process is not None and process.returncode not in (0, -15, 143):
        raise RuntimeError("worker failed: %s" % stderr)
finally:
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=2)
    command("quit")
    qmp.close()
PY

printf '%s\n' 'RESULT: DM MIT worker integration smoke passed'
