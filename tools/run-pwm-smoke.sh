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

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-pwm.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_elf="$run_dir/pwm.elf"
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
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_pwm_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_pwm_smoke.c"

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
    printf '%s\n' 'RESULT: PWM smoke failed (QMP socket missing)' >&2
    exit 1
}

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

def guest_marker():
    text = command("human-monitor-command", {
        "command-line": "xp /1wx 0x20000000"}) or ""
    values = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]
    return values[0] if values else 0

def qom_get(prop):
    return command("qom-get", {"path": "/machine", "property": prop})

command("cont")
for _ in range(3000):
    if guest_marker() == 0x50574D31:
        break
    time.sleep(0.001)
else:
    raise RuntimeError("PWM guest marker was not observed")

enabled = qom_get("buzzer-enabled")
level = qom_get("buzzer-level")
frequency = qom_get("buzzer-frequency-hz")
duty = qom_get("buzzer-duty-permille")
if enabled is not True:
    raise RuntimeError(f"PWM output was not enabled: {enabled!r}")
if frequency != "1000":
    raise RuntimeError(f"PWM frequency mismatch: {frequency!r}")
if duty != "250":
    raise RuntimeError(f"PWM duty mismatch: {duty!r}")
if not isinstance(level, bool):
    raise RuntimeError(f"PWM level is not boolean: {level!r}")
print("RESULT: TIM12 PWM lazy observer/QOM smoke passed")
print(f"  enabled={enabled} level={level} frequency={frequency} duty={duty}")
command("quit")
sock.close()
PY
