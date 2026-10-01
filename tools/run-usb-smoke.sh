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

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-usb.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/usb.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_usb_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_usb_smoke.c"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
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

deadline = time.monotonic() + 3.0
words = []
while time.monotonic() < deadline:
    text = command("human-monitor-command",
                   {"command-line": "xp /6wx 0x20000000"})
    words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 6 and words[0] == 0x55534231 and words[5] == 1:
        if words[1] != 0x4f54420a:
            raise RuntimeError("GSNPSID mismatch: %#x" % words[1])
        if words[2] != 1024:
            raise RuntimeError("GRXFSIZ mismatch: %#x" % words[2])
        if not (words[3] & (1 << 31)):
            raise RuntimeError("GRSTCTL AHBIDL is clear: %#x" % words[3])
        if not (words[4] & (1 << 31)):
            raise RuntimeError("AHBIDL is clear after CSRST: %#x" % words[4])
        if not (words[4] & (1 << 29)):
            raise RuntimeError("CSRSTDONE is clear after CSRST: %#x" % words[4])
        if words[4] & (1 << 0):
            raise RuntimeError("CSRST did not self-clear: %#x" % words[4])
        print("RESULT: DWC2 controller-ready bare-metal smoke passed")
        print("  GSNPSID=0x%08x GRXFSIZ=%u" % (words[1], words[2]))
        print("  GRSTCTL before=0x%08x after CSRST=0x%08x" %
              (words[3], words[4]))
        print("  Scope: controller-ready registers only; no USB enumeration,\n"
              "  endpoints, or bus model.")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("USB smoke result was not observed: %r" % words)

command("quit")
sock.close()
PY
