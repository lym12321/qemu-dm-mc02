#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-spi2-rx-reservation.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/spi2-rx-reservation.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_spi2_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_spi_rx_reservation_smoke.c"

"$qemu_bin" -machine dm-mc02,spi-dma-endpoint=on \
    -kernel "$guest_elf" -nodefaults -display none -monitor none \
    -serial none -qmp "unix:$qmp_socket,server=on,wait=off" \
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

deadline = time.monotonic() + 2.0
last = ""
while time.monotonic() < deadline:
    last = command("human-monitor-command",
                   {"command-line": "xp /11wx 0x20000000"})
    words = [int(value, 16) for value in
             re.findall(r"0x([0-9a-fA-F]{8})", last or "")]
    if len(words) >= 11 and words[0] == 0x444F4E45:
        if not (words[1] & 1):
            raise RuntimeError("RX pending flag was not retained: %r" % words)
        if words[2] != 1 or (words[3] & 1) or not (words[4] & (1 << 25)):
            raise RuntimeError("invalid-target DMA state mismatch: %r" % words)
        if words[5] == 0:
            raise RuntimeError("invalid-target guest wait timed out: %r" % words)
        if words[6] != 0x0F or words[7] != 0 or (words[8] & 1):
            raise RuntimeError("SPI RX reservation retry mismatch: %r" % words)
        if words[9] & (1 << 25) or words[10] == 0:
            raise RuntimeError("SPI RX retry completion state mismatch: %r" % words)
        print("RESULT: SPI2 RX DMA reservation/retry smoke passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("SPI2 RX reservation result was not observed: %r" % last)

command("quit")
sock.close()
PY
