#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-tim8-dma.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/tim8-dma.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_tim8_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_tim8_dma_smoke.c"

"$qemu_bin" -machine dm-mc02,accurate-timing=on -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -d guest_errors \
    -D /tmp/dm-mc02-tim8-dma.log \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
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

def words(command_line, width=8):
    text = command("human-monitor-command", {"command-line": command_line}) or ""
    return [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{%d})" % width, text)]

deadline = time.monotonic() + 3.0
last = None
while time.monotonic() < deadline:
    result = words("xp /7wx 0x20000000")
    waveform = words("xp /4hx 0x30000100", 4)
    ccr = words("xp /1wx 0x40010434")
    status = words("xp /1wx 0x40020404")
    last = (result, waveform, status)
    if len(result) >= 7 and len(waveform) >= 4 and result[0] == 0x54384432:
        if result[5] != 0x005400a8 or result[6] != 0x005400a8:
            raise RuntimeError("TIM8 initial waveform write mismatch: %r" % result[5:7])
        if waveform != [168, 84, 168, 84]:
            raise RuntimeError("TIM8 waveform buffer mismatch: %r" % waveform)
        # The guest writes the result marker before the first compare deadline;
        # allow the asynchronous timer/DMA path to reach the endpoint before
        # checking the transferred half-word.
        if not ccr or ccr[0] not in (168, 84):
            time.sleep(0.001)
            continue
        if not (1 <= result[1] <= 4) or not (result[2] & 1) or not (result[2] & (1 << 6)) or not (result[2] & (1 << 8)):
            time.sleep(0.001)
            continue
        if result[3] != 0x40010434 or not (0x30000100 <= result[4] < 0x30000108) or result[4] & 1:
            raise RuntimeError("TIM8 DMA endpoint mismatch: %r" % result[3:5])
        if not status or (status[0] & ((1 << 20) | (1 << 21))) != ((1 << 20) | (1 << 21)):
            raise RuntimeError("TIM8 DMA HT/TC flags missing: %r" % status)
        print("RESULT: TIM8 CH1 DMA circular smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("TIM8 DMA result was not observed: %r" % (last,))
command("quit")
sock.close()
PY
