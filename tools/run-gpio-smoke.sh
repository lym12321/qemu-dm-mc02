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
    printf '%s\n' 'blocked: python3 is required for QMP smoke' >&2
    exit 1
}
command -v timeout >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: timeout is required for bounded smoke' >&2
    exit 1
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU build not found: %s\n' "$qemu_bin" >&2
    exit 1
}

run_dir=$(mktemp -d "${TMPDIR:-/tmp}/dm-mc02-gpio-smoke.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/gpio-smoke.elf"
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
    -Wl,-T,"$root_dir/smoke/dm_mc02_gpio_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_gpio_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" >/dev/null \
    2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 100); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
if [[ ! -S "$qmp_socket" ]]; then
    printf '%s\n' 'QEMU did not create the QMP socket' >&2
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
fi

timeout 20s python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command_raw

response = command("human-monitor-command",
                   {"command-line": "xp /26wx 0x20000000"})
if "error" in response:
    raise RuntimeError("guest memory query failed: %s" % response)
words = [int(value, 16) for value in re.findall(
    r"0x([0-9a-fA-F]{8})", response.get("return", ""))]
expected = [0x47495031,
            1, 0x8001, 2, 2, 0x0d, 0x0d, 8, 8, 16, 16,
            0, 0, 9, 0, 0, 0x12345678, 0x9abcdef0, 1,
            0x1e, 0x0f, 9, 9, 0x0203, 0x8001, 0x0202]
if words[:len(expected)] != expected:
    raise RuntimeError("GPIO marker/readback mismatch: %r expected %r" %
                       (words[:len(expected)], expected))
print("RESULT: STM32H7 GPIO A-E bank smoke passed")
print("  marker/readback:", " ".join("0x%08x" % x for x in words[:len(expected)]))
command("quit")
PY
