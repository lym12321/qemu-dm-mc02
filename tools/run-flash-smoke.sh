#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-flash.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/flash.elf"
flash_file="$run_dir/internal-flash.bin"
bad_flash_file="$run_dir/short-flash.bin"
long_flash_file="$run_dir/long-flash.bin"
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
command -v timeout >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: timeout is required for invalid image checks' >&2
    exit 1
}
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_flash_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_flash_smoke.c"

dd if=/dev/zero bs=1048576 count=1 status=none | tr '\000' '\377' >"$flash_file"
: >"$bad_flash_file"
truncate -s 3 "$bad_flash_file"
: >"$long_flash_file"
truncate -s 1048577 "$long_flash_file"

"$qemu_bin" -machine "dm-mc02,flash-file=$flash_file" \
    -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /6wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 6 and words[0] == 0x464c5348:
        expected = [0x464c5348, 0x12345678, 0x00040000,
                    0x00010000, 0xffffffff, 1]
        if words[:6] != expected:
            raise RuntimeError(f"Flash smoke mismatch: {words[:5]!r}")
        print("RESULT: internal Flash program/sector erase smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("Flash smoke result was not observed")
if "error" in command("system_reset"):
    raise RuntimeError("Flash reset command failed")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /10wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 10 and words[9] == 0x52535432:
        expected = [0x00000001, 0xffffffff, 1, 0x52535432]
        if words[6:10] != expected:
            raise RuntimeError(f"Flash reset mismatch: {words[6:10]!r}")
        print("RESULT: Flash reset relocks and disables program overlay")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("Flash reset result was not observed")
command("quit")
PY

for bad_image in "$bad_flash_file" "$long_flash_file"; do
    image_name=${bad_image##*/}
    if timeout --signal=TERM --kill-after=1s 5s "$qemu_bin" \
        -machine "dm-mc02,flash-file=$bad_image" -nodefaults \
        -display none -S >"$run_dir/$image_name.stdout" \
        2>"$run_dir/$image_name.stderr"; then
        printf 'FAIL: invalid internal Flash image was accepted: %s\n' \
            "$image_name" >&2
        exit 1
    else
        status=$?
    fi
    [[ "$status" -ne 124 && "$status" -ne 137 ]] || {
        printf 'FAIL: invalid internal Flash image did not reject promptly: %s\n' \
            "$image_name" >&2
        sed -n '1,80p' "$run_dir/$image_name.stderr" >&2
        exit 1
    }
    grep -F "DM-MC02 internal Flash image '$bad_image' rejected" \
        "$run_dir/$image_name.stderr" >/dev/null || {
        printf 'FAIL: internal Flash image rejection was not reported: %s\n' \
            "$image_name" >&2
        sed -n '1,80p' "$run_dir/$image_name.stderr" >&2
        exit 1
    }
done
printf '%s\n' 'RESULT: internal Flash persistence load and exact-size rejection smoke passed'
