#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-board-profile.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -f -- "$qmp_socket"
    rm -f -- "$run_dir/qemu.stderr"
    rmdir -- "$run_dir"
}
trap cleanup EXIT

"$qemu_bin" -machine dm-mc02,board-profile=STM32H723-EVAL \
    -nodefaults -display none -monitor none -serial none -S \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 50); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.02
done
if [[ ! -S "$qmp_socket" ]]; then
    printf '%s\n' 'RESULT: alternate board profile QMP socket was not created' >&2
    sed -n '1,40p' "$run_dir/qemu.stderr" >&2
    exit 1
fi

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import sys

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

profile = command("qom-get", {"path": "/machine", "property": "board-profile"})
assert profile == "STM32H723-EVAL", profile
power = command("qom-get", {"path": "/machine", "property": "mcu-power-good"})
assert power is True, power

print("RESULT: alternate STM32H723 board profile QEMU smoke passed")
print("  profile=STM32H723-EVAL, UARTs=2, FDCAN=1, mcu-power-good=true")
PY
