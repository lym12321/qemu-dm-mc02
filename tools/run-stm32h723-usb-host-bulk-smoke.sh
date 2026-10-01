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

run_dir=$(mktemp -d "/tmp/dm-qemu.stm32h723-usb-host-bulk.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/usb-host-bulk.elf"
serial_socket="$run_dir/usb-serial.sock"
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
    -o "$guest_elf" "$root_dir/smoke/stm32h723_usb_host_bulk_smoke.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_control.c" \
    "$root_dir/firmware/dm_usb_host_descriptor.c" \
    "$root_dir/firmware/dm_usb_host_pipe.c" \
    "$root_dir/firmware/dm_usb_host_channel_allocator.c" \
    "$root_dir/firmware/dm_usb_host_endpoint_state.c" \
    "$root_dir/firmware/dm_usb_host_bulk.c" \
    "$root_dir/firmware/dm_usb_host_retry.c" \
    "$root_dir/firmware/dm_usb_host_channel_operation.c" \
    "$root_dir/firmware/dm_usb_host_channel_completion.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_pipe.c" \
    "$root_dir/firmware/dm_stm32h7_usb_host_bulk.c" \
    -lgcc

"$qemu_bin" -S -machine stm32h723-usb-host -kernel "$guest_elf" \
    -chardev "socket,id=serial,path=$serial_socket,server=on,wait=off" \
    -device usb-serial,bus=usb-bus.0,port=1,chardev=serial,always-plugged=on \
    -nodefaults -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" && -S "$serial_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" && -S "$serial_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$qmp_socket" "$serial_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import sys
import time

qmp_path, serial_path = sys.argv[1:]
serial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
serial.settimeout(2.0)
serial.connect(serial_path)
sock = QmpSession(qmp_path, timeout=2.0)

command = sock.command

command("cont")
deadline = time.monotonic() + 3.0
last = []
injected = False
while time.monotonic() < deadline:
    text = command("human-monitor-command", {
        "command-line": "xp /28wx 0x20000000"}) or ""
    last = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]
    if len(last) >= 28 and last[26] == 0x52454144 and not injected:
        command("stop")
        serial.sendall(bytes(range(10)))
        command("query-status")
        command("cont")
        injected = True
    if len(last) >= 28 and last[27] == 0x444F4E45:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("USB host bulk guest result was not observed: %r" % last)

if last[0] != 0x48534255:
    raise RuntimeError("guest marker mismatch: %r" % last)
if (last[1] & ((1 << 12) | (1 << 2) | 1)) != ((1 << 12) | (1 << 2) | 1):
    raise RuntimeError("HPRT0 was not powered, connected and enabled: %#x" % last[1])
if last[2] != 0 or last[3] != 0x112 or last[4] != 0:
    raise RuntimeError("USB serial EP0 discovery failed: %r" % last)
if last[5] != 0 or last[6] != 0x00010020 or last[7] != 0:
    raise RuntimeError("USB serial configuration discovery failed: %r" % last)
if last[8] != 0x40020281 or last[9] != 0:
    raise RuntimeError("USB serial bulk endpoint configuration failed: %r" % last)
if last[23] != 1:
    raise RuntimeError("idle bulk IN did not return NAK without mutation: %r" % last)
if last[24] != 0:
    raise RuntimeError("idle bulk IN left H723 channel enabled: %r" % last)
if last[10] != 0 or last[11] != 130 or last[12] != 1:
    raise RuntimeError("bulk OUT transfer result mismatch: %r" % last)
hcchar_mask = (0x7f << 22) | (3 << 18) | (1 << 15) | (0xf << 11) | 0x7ff
hcchar_expected = (5 << 22) | (2 << 18) | (2 << 11) | 64
if (last[13] & hcchar_mask) != hcchar_expected:
    raise RuntimeError("bulk channel HCCHAR fields mismatch: %r" % last)
if last[14] != 0:
    raise RuntimeError("bulk channel HCTSIZ final PID mismatch: %r" % last)
if last[15] != 0:
    raise RuntimeError("bulk channel lease was not released: %r" % last)

if last[16] != 0 or last[17] != 12 or last[18] != 1:
    raise RuntimeError("bulk IN transfer result mismatch: %r" % last)
hcchar_in_expected = (5 << 22) | (2 << 18) | (1 << 15) | (1 << 11) | 64
if (last[19] & hcchar_mask) != hcchar_in_expected:
    raise RuntimeError("bulk IN channel HCCHAR fields mismatch: %r" % last)
if last[20] != 52 or last[21] != 0 or last[22] != 0x010000B1:
    raise RuntimeError("bulk IN packet or payload mismatch: %r" % last)

payload = bytearray()
while len(payload) < 130:
    chunk = serial.recv(130 - len(payload))
    if not chunk:
        raise RuntimeError("USB serial chardev closed before OUT payload")
    payload.extend(chunk)
if payload != bytes(range(130)):
    raise RuntimeError("usb-serial OUT payload mismatch: %r" % payload)

print("RESULT: STM32H723 guest USB host bulk smoke passed")
print("  EP0 discovery -> bulk OUT/IN -> H723 PIO/QEMU/chardev path")
command("quit")
serial.close()
sock.close()
PY
