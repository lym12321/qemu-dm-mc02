#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 2; }

python3 - "$root_dir/tools/dm_mc02_sim_worker.py" <<'PY'
import importlib.util
import struct
import sys
spec = importlib.util.spec_from_file_location("dm_worker", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)
packet = worker.adc_voltage_frame(7, 123456, 19, 3300000, flags=1)
body_len = struct.unpack_from("<I", packet)[0]
body = packet[4:]
assert struct.unpack_from("<IHHIQQ", body) == (worker.MAGIC, worker.VERSION, 6, 12, 7, 123456)
assert struct.unpack_from("<HHII", body, worker.HEADER) == (19, 1, 3300000, 0)
assert body_len == worker.HEADER + 12
for args in ((32, 0), (19, -1), (19, 3300001)):
    try:
        worker.adc_voltage_frame(1, 1, args[0], args[1])
    except ValueError:
        pass
    else:
        raise AssertionError("invalid ADC voltage accepted: %r" % (args,))
try:
    worker.adc_voltage_frame(1, 1, 19, 1_000_000, flags=2)
except ValueError:
    pass
else:
    raise AssertionError("unsupported ADC voltage flags accepted")
print("RESULT: ADC voltage worker codec smoke passed")
PY

command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required for QEMU integration' >&2; exit 2; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 2; }
run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-analog.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
cosim_socket="$run_dir/cosim.sock"
guest_elf="$run_dir/adc-analog.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_adc_analog_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_adc_analog_smoke.c"
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
[[ -S "$cosim_socket" && -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 2; }

set +e
python3 - "$cosim_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time
cosim_path, qmp_path = sys.argv[1:]
def frame(kind, seq, timestamp, payload=b""):
    body = struct.pack("<IHHIQQ", 0x32434D44, 1, kind, len(payload), seq, timestamp) + payload
    return struct.pack("<I", len(body)) + body
def exact(sock, size):
    out = bytearray()
    while len(out) < size:
        out.extend(sock.recv(size - len(out)))
    return bytes(out)
cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.settimeout(2); cosim.connect(cosim_path)
outer = struct.unpack("<I", exact(cosim, 4))[0]; exact(cosim, outer)
cosim.sendall(frame(1, 0, 0))
qmp = QmpSession(qmp_path, timeout=2)
command = qmp.command
command("cont")
# The board power model starts at 24 V.  With the firmware's VIN/11
# divider, the ADC pin code is round(24/11/3.3*65535) = 43329; the
# unpressed LCD ladder is full scale.
deadline = time.monotonic() + 1.5
last = ""
while time.monotonic() < deadline:
    last = command("human-monitor-command", {"command-line": "xp /4hx 0x20000100"}) or ""
    samples = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{4})", last)]
    if samples[:4] == [43329, 65535, 43329, 65535]:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("default 24 V ADC result mismatch: %s" % last)

# External pin-voltage inputs override board sources without changing the
# firmware-selected sequence.
cosim.sendall(frame(6, 1, 1_000_000, struct.pack("<HHII", 4, 0, 1_650_000, 0)))
cosim.sendall(frame(6, 2, 2_000_000, struct.pack("<HHII", 19, 0, 825_000, 0)))
deadline = time.monotonic() + 1.5
last = ""
while time.monotonic() < deadline:
    last = command("human-monitor-command", {"command-line": "xp /4hx 0x20000100"}) or ""
    samples = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{4})", last)]
    if samples[:4] == [32768, 16384, 32768, 16384]:
        print("RESULT: ADC analog voltage SQR1/DMA smoke passed")
        command("quit"); sys.exit(0)
    time.sleep(0.01)
raise RuntimeError("ADC analog result mismatch: %s" % last)
PY
status=$?
set -e
if [[ $status -eq 0 ]]; then exit 0; fi
printf '%s\n' 'ADC voltage integration smoke failed' >&2
exit "$status"
