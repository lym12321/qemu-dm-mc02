#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'RESULT: blocked (arm-none-eabi-gcc is required)' >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'RESULT: blocked (python3 is required)' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || {
    printf 'RESULT: blocked (QEMU not found: %s)\n' "$qemu_bin" >&2
    exit 1
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-dma-irq.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/dma-irq.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_dma_irq_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_dma_irq_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none -S \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || {
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    printf '%s\n' 'RESULT: blocked (QEMU did not create QMP socket)' >&2
    exit 1
}

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

command("cont")
deadline = time.monotonic() + 2.0
last = None
while time.monotonic() < deadline:
    text = command("human-monitor-command", {
        "command-line": "xp /10wx 0x20000000"
    })
    words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text or "")]
    last = words
    if (len(words) >= 10 and words[0] == 0x44495251 and
            words[1:3] == [0x12345678, 0x9abcdef0] and
            words[3] == 0x49525131 and words[5] == 0 and
            words[6] == 0x49525131 and words[7] == 0):
        if words[4] & (1 << 5) == 0:
            raise RuntimeError("handler did not observe Stream0 HTIF+TCIF: 0x%08x" %
                               words[4])
        if words[4] != ((1 << 5) | (1 << 4)):
            raise RuntimeError("handler did not observe Stream0 HTIF+TCIF: 0x%08x" %
                               words[4])
        print("RESULT: DMA1 Stream0 IRQ smoke passed")
        print("  target: 0x%08x 0x%08x" % (words[1], words[2]))
        print("  handler marker: 0x%08x" % words[6])
        print("  LISR before/after clear: 0x%08x / 0x%08x" %
              (words[4], words[7]))
        break
    time.sleep(0.01)
else:
    raise RuntimeError("DMA IRQ result was not observed: %r" % last)
command("quit")
sock.close()
PY
