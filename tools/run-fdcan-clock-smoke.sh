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

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-fdcan-clock.XXXXXX")
trap 'rm -rf -- "$run_dir"' EXIT

for source in 0 1 2 3; do
    guest_elf="$run_dir/fdcan-clock-$source.elf"
    arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
        -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
        -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_fdcan_smoke.ld" \
        -D FDCAN_SOURCE="$source" -o "$guest_elf" \
        "$root_dir/smoke/dm_mc02_fdcan_clock_smoke.c"

    python3 "$script_dir/dm_mc02_test_harness.py" --expect-qemu-quit \
        --socket qmp --python-arg "{qmp}" --python-arg "$source" -- \
        "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
        -display none -monitor none -serial none -S \
        -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import sys

path, source = sys.argv[1:]
source = int(source)
expected = {0: 24_000_000, 1: 120_000_000, 2: 96_000_000, 3: 0}[source]
sock = QmpSession(path, timeout=2.0)

command = sock.command

command("cont")
deadline = __import__("time").monotonic() + 2.0
while __import__("time").monotonic() < deadline:
    text = command("human-monitor-command", {"command-line":
                                              "xp /2wx 0x20000000"}) or ""
    if "0x4644434c" in text.lower():
        break
    __import__("time").sleep(0.01)
else:
    raise RuntimeError("FDCAN clock guest did not configure RCC: %r" % text)
clock = command("qom-get", {"path": "/machine",
                             "property": "fdcan-kernel-clock-hz"})
if clock != expected:
    raise RuntimeError("FDCAN source %d: expected %d Hz, got %r" %
                       (source, expected, clock))
print("FDCAN source %d: %d Hz" % (source, clock))
command("quit")
sock.close()
PY
done

printf '%s\n' 'RESULT: FDCAN RCC kernel-clock source smoke passed'
