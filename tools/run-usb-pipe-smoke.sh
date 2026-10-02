#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-usb-pipe.XXXXXX")
guest_elf="$run_dir/usb-pipe.elf"
trap 'rm -rf -- "$run_dir"' EXIT

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_usb_pipe_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_usb_pipe_smoke.c"

serial_args=()
for _ in $(seq 1 10); do serial_args+=( -serial null ); done
serial_args+=( -serial "unix:{usb},server=on,wait=off" )

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp --socket usb \
    --python-arg "{qmp}" --python-arg "{usb}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none "${serial_args[@]}" \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import sys
import time

qmp = QmpSession(sys.argv[1], timeout=2.0)
usb = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
usb.settimeout(2.0)
usb.connect(sys.argv[2])

qcmd = qmp.command

usb.settimeout(1.0)
usb.sendall(b"PING")
out = usb.recv(3)
if out != b"OK-":
    raise RuntimeError(f"USB host output mismatch: {out!r}")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = qcmd("human-monitor-command", {"command-line": "xp /2wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 2 and words[0] == 0x55534250:
        if words[1] != 0x474e4950:
            raise RuntimeError(f"USB guest input mismatch: {words[:2]!r}")
        print("RESULT: virtual USB CDC byte-pipe smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("USB pipe result was not observed")
qcmd("quit")
PY
