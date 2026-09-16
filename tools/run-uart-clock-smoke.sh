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

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-uart-clock.XXXXXX")
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

# source expected16 expected234 hsi_div apb1_encoding apb2_encoding
cases=(
    "0 16000000 8000000 1 5 4"
    "1 96000000 96000000 1 5 4"
    "2 60000000 60000000 1 5 4"
    "3 32000000 32000000 1 5 4"
    "4 4000000 4000000 1 5 4"
    "5 32768 32768 1 5 4"
    "6 0 0 1 5 4"
    "0 8000000 16000000 1 4 5"
    "3 16000000 16000000 2 5 4"
)

case_index=0
for test_case in "${cases[@]}"; do
    read -r source expected16 expected234 hsi_div apb1_div apb2_div <<< "$test_case"
    qmp_socket="$run_dir/qmp-$case_index.sock"
    guest_elf="$run_dir/uart-clock-$case_index.elf"
    arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
        -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
        -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_uart_clock_smoke.ld" \
        -D UART_SOURCE="$source" -D UART_HSI_DIV="$hsi_div" \
        -D UART_APB1_DIV="$apb1_div" -D UART_APB2_DIV="$apb2_div" \
        -o "$guest_elf" "$root_dir/smoke/dm_mc02_uart_clock_smoke.c"

    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
        -display none -monitor none -serial none -S \
        -qmp "unix:$qmp_socket,server=on,wait=off" \
        >/dev/null 2>"$run_dir/qemu-$case_index.stderr" &
    qemu_pid=$!
    for _ in $(seq 1 300); do
        [[ -S "$qmp_socket" ]] && break
        sleep 0.01
    done
    [[ -S "$qmp_socket" ]] || {
        sed -n '1,80p' "$run_dir/qemu-$case_index.stderr" >&2
        exit 1
    }

    python3 - "$qmp_socket" "$source" "$expected16" "$expected234" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

path, source, expected16, expected234 = sys.argv[1:]
source = int(source)
expected = (int(expected16), int(expected234))
sock = QmpSession(path, timeout=2.0)

command = sock.command

def words():
    text = command("human-monitor-command", {
        "command-line": "xp /2wx 0x20000000"}) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

command("cont")
deadline = time.monotonic() + 2.0
current = []
while time.monotonic() < deadline:
    current = words()
    if len(current) >= 2 and current[0] == 0x55434C4B:
        break
    time.sleep(0.0002)
else:
    raise RuntimeError("guest RCC setup marker was not observed: %r" % current)

actual = (
    command("qom-get", {"path": "/machine",
                         "property": "usart16-kernel-clock-hz"}),
    command("qom-get", {"path": "/machine",
                         "property": "usart234578-kernel-clock-hz"}),
)
if actual != expected:
    raise RuntimeError("source %d: expected %r, got %r" %
                       (source, expected, actual))
print("source %d: USART16=%d USART234578=%d" %
      (source, actual[0], actual[1]))
command("quit")
sock.close()
PY
    wait "$qemu_pid" || true
    qemu_pid=''
    case_index=$((case_index + 1))
done

printf '%s\n' 'RESULT: USART RCC kernel-clock source matrix smoke passed'
