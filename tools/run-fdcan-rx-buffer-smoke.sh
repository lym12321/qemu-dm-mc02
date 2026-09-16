#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || exit 1
[[ -x "$qemu_bin" ]] || exit 1

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-fdcan-rx-buffer.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
can_socket="$run_dir/can1.sock"
guest_elf="$run_dir/rx-buffer.elf"
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
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_fdcan_rx_buffer_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_fdcan_rx_buffer_smoke.c"

serial_args=()
for _ in $(seq 1 7); do
    serial_args+=(-serial none)
done
"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S "${serial_args[@]}" \
    -chardev "socket,id=can1,path=$can_socket,server=on,wait=off" \
    -serial chardev:can1 -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" && -S "$can_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" && -S "$can_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

python3 - "$qmp_socket" "$can_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import sys
import time

qmp_path, can_path = sys.argv[1:]
qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

def words():
    text = command("human-monitor-command", {
        "command-line": "xp /5wx 0x20000000",
    }) or ""
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]

def dump(address, count):
    text = command("human-monitor-command", {
        "command-line": "xp /%dwx 0x%x" % (count, address),
    }) or ""
    return [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]

def wire(can_id, payload, flags=0):
    return (can_id.to_bytes(4, "little") + flags.to_bytes(4, "little") +
            bytes([len(payload), 0, 0, 0]) + (0).to_bytes(8, "little") +
            payload + bytes(64 - len(payload)))

command("cont")
can = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
can.settimeout(2.0)
can.connect(can_path)
time.sleep(0.05)
can.sendall(wire(0x321, bytes.fromhex("1122334455667788")))

deadline = time.monotonic() + 2.0
std_seen = False
second_seen = False
while time.monotonic() < deadline:
    value = words()
    if value and value[0] == 0x52425831:
        print("RESULT: FDCAN standard/extended dedicated Rx Buffer smoke passed")
        break
    if value and value[0] == 0x52425832 and not std_seen:
        std_seen = True
        can.sendall(wire(0x321, bytes.fromhex("aabbccddeeff0011")) +
                    wire(0x322, bytes(8)))
    if value and value[0] == 0x52425833 and not second_seen:
        second_seen = True
        can.sendall(wire(0x321, bytes.fromhex("aabbccddeeff0011")))
    if value and value[0] == 0x52425834:
        can.sendall(wire(0x1234567, bytes.fromhex("1122334455667788"), 1))
    if value and value[0] == 0x52425846:
        raise RuntimeError("guest rejected dedicated Rx Buffer frame: %r regs=%r target=%r" %
                           (value, dump(0x4000a080, 16), dump(0x4000ae50, 4)))
    time.sleep(0.005)
else:
    raise RuntimeError("dedicated Rx Buffer frame was not observed: result=%r fdcan=%r filter=%r buffer=%r" %
                       (value, dump(0x4000a098, 4), dump(0x4000ad00, 2),
                        dump(0x4000ae50, 4)))

can.close()
command("quit")
qmp.close()
PY
