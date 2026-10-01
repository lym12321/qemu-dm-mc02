#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 1
}

run_dir=$(mktemp -d "/tmp/dm-qemu.stm32h723-usb-host-async.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/usb-host-async.elf"
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
    -Wl,--build-id=none -I"$root_dir/firmware" \
    -Wl,-T,"$root_dir/smoke/stm32h723_usb_host_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/stm32h723_usb_host_async_smoke.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_control.c" \
    "$root_dir/firmware/dm_stm32h7_irq_lock.c" \
    "$root_dir/firmware/dm_usb_host_descriptor.c" \
    "$root_dir/firmware/dm_usb_host_pipe.c" \
    "$root_dir/firmware/dm_usb_host_channel_allocator.c" \
    "$root_dir/firmware/dm_usb_host_endpoint_state.c" \
    "$root_dir/firmware/dm_usb_host_channel_operation.c" \
    "$root_dir/firmware/dm_usb_host_channel_completion.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_pipe.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_pipe_async_dispatch.c" \
    -lgcc

"$qemu_bin" -machine stm32h723-usb-host -kernel "$guest_elf" \
    -device usb-kbd,bus=usb-bus.0,port=1 -nodefaults -display none \
    -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

deadline = time.monotonic() + 3.0
last = []
while time.monotonic() < deadline:
    output = command("human-monitor-command", {
        "command-line": "xp /25wx 0x20000000"}) or ""
    last = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", output)]
    if len(last) >= 25 and last[24] == 0x444F4E45:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("USB host async guest result was not observed: %r" % last)

if last[0] != 0x48534155:
    raise RuntimeError("guest marker mismatch: %r" % last)
if (last[1] & ((1 << 12) | (1 << 2) | 1)) != ((1 << 12) | (1 << 2) | 1):
    raise RuntimeError("HPRT0 was not powered, connected and enabled: %#x" % last[1])
if last[2] != 0 or last[3] != 0x112:
    raise RuntimeError("GET_DESCRIPTOR failed: %r" % last)
if last[4] != 0 or last[5] != 0 or last[6] != 0x00010022:
    raise RuntimeError("SET_ADDRESS/configuration header failed: %r" % last)
if last[7] != 0 or last[8] != 0x00080381 or last[9] != 0:
    raise RuntimeError("configuration discovery failed: %r" % last)
if last[10] != 0 or last[11] != 0:
    raise RuntimeError("async channels did not start: %r" % last)
if last[12] == 0 or last[13] == 0:
    raise RuntimeError("async completion tokens are invalid: %r" % last)
if last[14] != 1 or last[15] != 3 or last[16] != 1:
    raise RuntimeError("channel cancellation state mismatch: %r" % last)
if last[17] != 2 or last[18] != 1 or last[19] != 0:
    raise RuntimeError("channel 1 NAK completion mismatch: %r" % last)
if last[20] != 1 or last[21] != 1:
    raise RuntimeError("guest IRQ handler did not consume exactly one completion: %r" % last)
if last[22] != 0 or last[23] != 0:
    raise RuntimeError("async channel resources were not released: %r" % last)

print("RESULT: STM32H723 guest USB async IRQ smoke passed")
print("  synchronous EP0 enumeration -> two async channels -> cancel + IRQ NAK")
command("quit")
sock.close()
PY
