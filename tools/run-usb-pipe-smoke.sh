#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
run_dir=$(mktemp -d "$root_dir/output.dm-mc02-usb-pipe.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
usb_socket="$run_dir/usb.sock"
guest_elf="$run_dir/usb-pipe.elf"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

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
serial_args+=( -serial "unix:$usb_socket,server=on,wait=off" )
"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none "${serial_args[@]}" \
    -qmp "unix:$qmp_socket,server=on,wait=off" >/dev/null \
    2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" && -S "$usb_socket" ]] && break
    sleep 0.01
done
[[ -S "$usb_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$qmp_socket" "$usb_socket" <<'PY'
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
