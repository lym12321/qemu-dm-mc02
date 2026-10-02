#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
if (( $# == 0 )); then
    for mode in on off; do
        bash "${BASH_SOURCE[0]}" "$mode"
    done
    exit 0
fi
spi_dma_endpoint=${1:-on}
case "$spi_dma_endpoint" in
    on|off) ;;
    *)
        printf 'usage: %s [on|off]\n' "${BASH_SOURCE[0]}" >&2
        exit 2
        ;;
esac
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-spi2-dma.XXXXXX")
guest_elf="$run_dir/spi2-dma.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_spi2_dma_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_spi2_dma_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" --python-arg "$spi_dma_endpoint" -- \
    "$qemu_bin" -machine "dm-mc02,spi-dma-endpoint=$spi_dma_endpoint" \
    -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -d guest_errors -D /tmp/dm-mc02-spi2-dma.log \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /14wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if (len(words) >= 14 and words[0] == 0x53324432 and
            words[10] == 1 and words[13] == 1 and words[3] == 0 and
            words[4] == 0):
        if words[1:5] != [0, 0x0f, 0, 0]:
            raise RuntimeError("BMI088 DMA RX mismatch: %r full=%r" %
                               (words[1:5], words[:11]))
        if words[5] != 0xc000000 or words[6] != 0x30 or words[7] & 3:
            raise RuntimeError("DMA completion state mismatch: %r" % words[5:8])
        if words[8] != 0 or words[9] != 0:
            raise RuntimeError("DMA address/base mismatch: %r" % words[8:10])
        if words[11] != 0xa5 or words[12] != 2:
            raise RuntimeError("DMA PINC endpoint mismatch: %r" % words[11:13])
        if words[10] == 0:
            raise RuntimeError("DMA smoke timed out")
        print("RESULT: SPI2 BMI088 DMA request smoke passed (%s)" % sys.argv[2])
        break
    time.sleep(0.01)
else:
    raise RuntimeError("SPI2 DMA result was not observed: %r" % words)
command("quit")
sock.close()
PY
