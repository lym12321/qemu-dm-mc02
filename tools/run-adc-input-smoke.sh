#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-input.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
cosim_socket="$run_dir/cosim.sock"
guest_elf="$run_dir/adc-input.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_adc_input_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_input_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
    -serial chardev:cosim -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$cosim_socket" && -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$cosim_socket" && -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
}

timeout 20s python3 - "$cosim_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import socket
import struct
import sys
import time

COSIM, QMP = sys.argv[1:]
MAGIC = 0x32434D44
HEADER = 28
RESET, ADC_INPUT = 1, 5

def frame(kind, sequence, timestamp, payload=b""):
    body = struct.pack("<IHHIQQ", MAGIC, 1, kind, len(payload),
                       sequence, timestamp) + payload
    return struct.pack("<I", len(body)) + body

def read_words(sock, address, count, width="wx"):
    text = sock.command("human-monitor-command",
                       {"command-line": f"xp /{count}{width} 0x{address:x}"}) or ""
    import re
    digits = 8 if width == "wx" else 4
    return [int(value, 16) for value in
            re.findall(r"0x([0-9a-fA-F]{%d})" % digits, text)]

cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.settimeout(2.0)
cosim.connect(COSIM)
outer = struct.unpack("<I", cosim.recv(4))[0]
if outer != HEADER + 12:
    raise RuntimeError("missing initial telemetry")
packet = bytearray()
while len(packet) < outer:
    packet.extend(cosim.recv(outer - len(packet)))

# Start a host->QEMU session, then inject VIN/channel 4 and LCD/channel 19.
cosim.sendall(frame(RESET, 0, 0))
cosim.sendall(frame(ADC_INPUT, 1, 1_000_000, struct.pack("<HHI", 4, 0x1234, 0)))
cosim.sendall(frame(ADC_INPUT, 2, 2_000_000, struct.pack("<HHI", 19, 0xabcd, 0)))

qmp = QmpSession(QMP, timeout=2.0)
qmp.command("cont")

deadline = time.monotonic() + 3.0
last = None
while time.monotonic() < deadline:
    result = read_words(qmp, 0x20000000, 6)
    samples = read_words(qmp, 0x20000100, 4, "hx")
    last = (result, samples)
    if (len(result) >= 6 and len(samples) >= 4 and
            result[0] == 0x41444931 and
            samples == [0x1234, 0xabcd, 0x1234, 0xabcd]):
        # The rank-level ADC model can be between circular DMA requests when
        # the QMP snapshot is taken.  In accurate-timing mode the first
        # conversion may also complete before the guest has finished the
        # post-start diagnostic stores; keep polling until those stores are
        # visible instead of treating that valid ordering as a failure.
        if result[3] != 0x13101 or not (1 <= result[1] <= 4):
            time.sleep(0.001)
            continue
        print("RESULT: ADC external input and channel sequence smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("ADC input result mismatch: %r" % (last,))

qmp.command("quit")
qmp.close()
cosim.close()
PY
