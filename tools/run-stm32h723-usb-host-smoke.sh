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

run_dir=$(mktemp -d "$root_dir/output.stm32h723-usb-host.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/usb-host.elf"
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
    -o "$guest_elf" "$root_dir/smoke/stm32h723_usb_host_smoke.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_control.c" \
    "$root_dir/firmware/dm_usb_host_descriptor.c" \
    "$root_dir/firmware/dm_usb_host_pipe.c" \
    "$root_dir/firmware/dm_usb_host_channel_allocator.c" \
    "$root_dir/firmware/dm_usb_host_endpoint_state.c" \
    "$root_dir/firmware/dm_usb_host_periodic_schedule.c" \
    "$root_dir/firmware/dm_usb_host_periodic_poller.c" \
    "$root_dir/firmware/dm_usb_host_retry.c" \
    "$root_dir/firmware/dm_usb_host_channel_operation.c" \
    "$root_dir/firmware/dm_usb_host_periodic_registry.c" \
    "$root_dir/firmware/dm_usb_host_sof_clock.c" \
    "$root_dir/firmware/dm_usb_host_channel_completion.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_pipe.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_periodic.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_sof.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_periodic_registry_sof.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_sof_irq.c" \
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
    text = command("human-monitor-command", {
    "command-line": "xp /22wx 0x20000000"}) or ""
    last = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]
    if len(last) >= 22 and last[21] == 0x444F4E45:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("USB host guest result was not observed: %r" % last)

if last[0] != 0x48535455:
    raise RuntimeError("guest marker mismatch: %r" % last)
if (last[1] & ((1 << 12) | (1 << 2) | 1)) != ((1 << 12) | (1 << 2) | 1):
    raise RuntimeError("HPRT0 was not powered, connected and enabled: %#x" % last[1])
if last[2] != 0 or last[3] != 0x112:
    raise RuntimeError("GET_DESCRIPTOR failed: %r" % last)
if last[4] != 0 or last[5] != 0 or last[6] != 0:
    raise RuntimeError("SET_ADDRESS/GET_STATUS failed: %r" % last)
if last[7] != 0 or last[8] != 0x00010022 or last[9] != 0:
    raise RuntimeError("GET_CONFIGURATION failed: %r" % last)
if last[10] != 0x00080381 or last[11] != 0:
    raise RuntimeError("endpoint discovery/SET_CONFIGURATION failed: %r" % last)
if last[12] != 1:
    raise RuntimeError("idle interrupt IN did not return NAK: %r" % last)
if last[13] != 0:
    raise RuntimeError("NAK unexpectedly advanced endpoint PID: %r" % last)
if last[14] != 2 or last[15] != 0:
    raise RuntimeError("periodic poll did not submit once then defer: %r" % last)
if last[16] != 1 or last[17] != 1:
    raise RuntimeError("next periodic SOF slot did not submit a NAK poll: %r" % last)
if last[18] == 0:
    raise RuntimeError("periodic event loop did not observe a SOF event: %r" % last)
hcchar_mask = (0x7f << 22) | (3 << 18) | (1 << 15) | (0xf << 11) | 0x7ff
hcchar_expected = (5 << 22) | (3 << 18) | (1 << 15) | (1 << 11) | 8
if (last[19] & hcchar_mask) != hcchar_expected:
    raise RuntimeError("leased channel 1 HCCHAR fields mismatch: %r" % last)
if last[20] != 0:
    raise RuntimeError("periodic lease was not released after synchronous PIO: %r" % last)

print("RESULT: STM32H723 guest USB host-control smoke passed")
print("  EP0 discovery -> dynamic channel lease -> periodic NAK -> SOF-IRQ registry NAK")
command("quit")
sock.close()
PY
